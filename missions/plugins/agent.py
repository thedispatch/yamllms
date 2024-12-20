import json, time
from datetime import timedelta
from .github import *
from ..prompts import get_prompt_from_github
from ..models import TaskCategory, Task, TaskStatus
from ..util import *
from missions.apps import get_plugin_manager
from missions import plugins

MAX_AGENT_ITERATIONS = 16
MAX_AGENT_FILES = 32


@plugins.hookimpl
def run_agent(task):
    url = task.url or ""
    if url == AGENT_REPORT_URL:
        next_task = run_agent_final_report(task)
    elif url.startswith(ASSESS_RISK_URL):
        next_task = assess_risk_agent(task)
    else:  # generic agent
        next_task = answer_agent(task)

    if next_task:
        next_task.status = TaskStatus.IN_PROCESS
        next_task.save()
        time.sleep(settings.TASK_CREATION_DELAY)  # don't overload APIs
        task.mark_complete()
        return next_task


@plugins.hookimpl
def run_rating(task):
    rate_prs = task.url and task.url.startswith(GITHUB_PREFIX) and "/pulls" in task.url
    rate_prs = rate_prs or task.parent and task.parent.url.endswith("/pulls")
    if rate_prs:
        return assess_prs(task)


# Chat with an LLM using the appropriate provided plugin if any
def chat_llm(task, input, tool_key=""):
    pm = get_plugin_manager()
    completion = pm.hook.chat_llm(task=task, input=input, tool_key=tool_key)
    if not completion:
        raise Exception("No implementation for LLM chat available: %s" % task)
    return completion


def create_subsequent_task(task, iteration=0):
    new_name = task.name.split(" Iteration")[0]
    new_name += " Iteration %s" % iteration
    url = task.url or ""
    if url.split("/")[-1].isnumeric():
        url = url.split("/")[:-1] + [str(iteration)]
        url = "/".join(url)
    return Task.objects.create(
        mission=task.mission,
        task_info=task.task_info,
        category=task.category,
        order=task.order,
        visibility=task.visibility,
        llm=task.llm,
        extras=task.extras,
        flags=task.flags,
        parent=task,
        url=url,
        name=new_name,
    )


def run_agent_final_report(task):
    tool_key = task.flags.get("tool_key")
    chat_llm(task, task.parent.response, tool_key=tool_key)
    task.mark_complete()
    return Task.objects.create(
        name="Quantify Risks",
        mission=task.mission,
        order=task.order + 1,
        visibility=task.visibility,
        category=TaskCategory.QUANTIFIED_REPORT,
        flags=task.flags,
        parent=task,
        url=QUANTIFY_RISK_URL,
    )


def answer_agent(task):
    log("Running recursive agent task", task)
    dep = task.parent
    if not dep:  # initial task
        task.mark_complete()
        return create_subsequent_task(task)
    today = datetime.datetime.now().date().strftime("%Y-%m-%d")
    question = task.extras.get("agent_question")
    last_question = task.extras.get("report_question") or question

    # get all the previous datasets for this mission
    dataset_depth = task.flags.get("dataset_depth", 10)
    tasks = list(task.mission.task_set.filter(status=TaskStatus.COMPLETE))
    previous = task.mission.previous
    depth = 0
    while previous and depth < dataset_depth:
        tasks += list(previous.task_set.filter(status=TaskStatus.COMPLETE))
        previous = previous.previous
        depth += 1

    # fetches only
    valid_categories = [
        TaskCategory.API,
        TaskCategory.SCRAPE,
        TaskCategory.FILTER,
    ]
    if task.flags.get("include_quantified"):
        valid_categories.append(TaskCategory.QUANTIFIED_REPORT)
    datasets = [t for t in tasks if t.category in valid_categories]

    # include/exclude urls
    if task.flags.get("include_exclude_urls"):
        urls = task.flags.get("include_exclude_urls").split(",")
        urls = [u.strip().lower() for u in urls]
        if task.flags.get("include_exclude") == "include":
            datasets = [t for t in datasets if (t.url or "").lower() in urls]
        else:
            datasets = [t for t in datasets if (t.url or "").lower() not in urls]

    tools = set()
    for t in datasets:
        source = source_from_task(t)
        if source:
            tools.add(source["name"])

    # previous analysis
    previous_ids = dep.structured_data.get("previous_ids", []) if dep else []
    previous_tasks = [Task.objects.get(id=p) for p in previous_ids]
    datasets = [t for t in datasets if t.id not in previous_ids]
    analysis_so_far = dep.response or "" if dep else "No analysis yet"

    # current data to analyze
    data_task_id = dep.structured_data.get("next_id") if dep else None
    data_task = Task.objects.get(id=data_task_id) if data_task_id else None
    data_to_analyze = data_task.response if data_task else None

    base_prompt = task.flags.get("base_prompt", "detective")
    base_prompt = get_prompt_from_github(base_prompt)
    task.prompt = base_prompt % (
        today,
        question,
        "- " + "\n- ".join(sorted(list(tools))),
        "- " + "\n- ".join([task_to_line_for_llm(t) for t in datasets]),
        "- " + "\n- ".join([task_to_line_for_llm(t) for t in previous_tasks]),
        analysis_so_far or "None yet",
        task_to_line_for_llm(data_task) if data_task else "None yet",
    )
    chat_llm(task, data_to_analyze, tool_key="detective_report")

    retval = json.loads(get_json_from(task.response))
    if isinstance(retval, dict) and len(retval) == 1:
        # sometimes we get a dict with a single key, unwrap it
        sole_key = list(retval.keys())[0]
        new_retval = retval[sole_key]
        if isinstance(new_retval, dict) and len(new_retval) > 1:
            log("Unwrapping dict", sole_key)
            retval = new_retval

    new_previous_ids = previous_ids + [data_task_id] if data_task_id else previous_ids
    task.structured_data = {
        "response": retval,
        "previous_ids": new_previous_ids,
        "this_id": data_task_id,
        "next_id": retval.get("next_id"),
    }

    analysis_so_far = "" if analysis_so_far == "No analysis yet" else analysis_so_far
    assessment = ""
    new_assessment = ""
    if data_to_analyze:
        assessment = "\n\n---\n\n"
        assessment += "Analysis of %s:\n\n" % task_to_line_for_llm(data_task)
        new_assessment = retval.get("dataset_assessment", "")
        if not new_assessment:
            for key in retval.keys():
                if key.startswith("dataset") or key.startswith("assessment"):
                    new_assessment = "%s" % retval[key]
                    break
        assessment += new_assessment
    task.response = analysis_so_far + assessment
    task.save()

    consider_rerun = (
        data_task
        and task.flags.get("rerun_agent_tasks") == "true"
        and ("estimated_significance" not in retval or not new_assessment)
    )
    if consider_rerun and task.extras.get("reruns", 0) <= 3:
        task.extras["reruns"] = task.extras.get("reruns", 0) + 1
        task.status = TaskStatus.IN_PROCESS
        task.save()
        log("No estimated significance, trying rerun", task.extras["reruns"])
        return task

    task.mark_complete()
    task.extras.pop("reruns", None)
    next_iteration = 1
    if "Iteration" in task.name:
        next_iteration = 1 + int(task.name.split("Iteration")[1].strip())

    max_iterations = task.flags.get("max_iterations", MAX_AGENT_ITERATIONS)
    if (
        next_iteration <= max_iterations
        and int(retval.get("estimated_significance", 0)) >= 3
    ):
        return create_subsequent_task(task, iteration=next_iteration)
    else:
        report_prompt = get_prompt_from_github("detective-report")
        report_prompt = report_prompt % (
            today,
            last_question,
            "\n- ".join([task_to_line_for_llm(t) for t in previous_tasks]),
        )
        report_name = task.name.split(" Iteration")[0]
        report_name = report_name.replace("Detective", "Report")
        report_name = report_name.replace("Agent", "Report")
        agent_report = create_subsequent_task(task)
        agent_report.url = AGENT_REPORT_URL
        agent_report.prompt = report_prompt
        agent_report.name = report_name or "Agent Report"
        agent_report.save()
        return agent_report


def assess_risk_agent(task):
    log("Running recursive risk assessment task", task)
    dep = task.parent
    if not dep:  # initial task
        task.mark_complete()
        return create_subsequent_task(task)

    dataset_mission_ids = [task.mission.id]
    if task.mission.previous:
        dataset_mission_ids.append(task.mission.previous.id)
    datasets = Task.objects.filter(mission_id__in=dataset_mission_ids)

    # filter out any tasks that are not helpful, e.g. agent analyses
    datasets = datasets.exclude(category=TaskCategory.AGENT_TASK)

    # previous analysis
    previous_ids = dep.structured_data.get("previous_sources", [])
    datasets = datasets.exclude(id__in=previous_ids).only("id", "name", "created_at")

    # other current state
    previous_files = dep.structured_data.get("previous_files", [])
    max_iterations = task.flags.get("max_iterations", MAX_AGENT_ITERATIONS)
    analysis_so_far = dep.response or ""

    # TODO: look at the commits / PR data and add / replace with most recently edited files
    files = dep.structured_data.get("files", [])
    if not files:
        log("listing files from repo", task.get_repo())
        repo = get_gh_repo(task)
        main = repo.get_branch(repo.default_branch)
        tree = repo.get_git_tree(main.commit.sha, recursive=True)
        paths = get_tree_paths(tree, max_files=2048)
        # return the 32 largest files
        paths = sorted(paths, key=lambda x: x[1], reverse=True)[:MAX_AGENT_FILES]
        files = [p[0] for p in paths]

    # current data to analyze
    data_type = dep.structured_data.get("next_data_type", "task")
    data_id = dep.structured_data.get("next_data_id", "")
    if isinstance(data_id, str):
        data_id = data_id.replace("ID ", "")
    data_line = ""
    data_to_analyze = ""
    llm_response = {}
    new_previous_ids = previous_ids
    new_previous_files = previous_files

    if data_id:
        if data_type == "dataset" or data_type == "task":
            log("Fetching task", data_id)
            task_id = int(data_id)
            data_task = Task.objects.get(id=task_id)
            data_to_analyze = data_task.response
            new_previous_ids = previous_ids + [data_id]
            data_line = task_to_line_for_llm(data_task)
        elif data_type == "file":
            log("Fetching file", data_id)
            repo = get_gh_repo(task)
            try:
                data_to_analyze = get_gh_file(repo, {"path": data_id})
            except Exception as e:
                data_to_analyze = "File not found"
                log("Error fetching file", data_id, e)
            new_previous_files = previous_files + [data_id]
            data_line = data_id

    # analyze the new data in light of the old data
    previous_tasks = [Task.objects.get(id=p) for p in previous_ids]
    today = datetime.datetime.now().date().strftime("%Y-%m-%d")
    base_prompt = task.flags.get("base_prompt", "risk-analysis")
    base_prompt = get_prompt_from_github(base_prompt)
    task.prompt = base_prompt % (
        today,
        max_iterations,
        "- " + "\n- ".join([task_to_line_for_llm(t) for t in datasets]),
        "- " + "\n- ".join([f for f in files]),
        "- " + "\n- ".join([task_to_line_for_llm(t) for t in previous_tasks]),
        "- " + "\n- ".join([f for f in new_previous_files]),
        analysis_so_far or "None yet",
        data_line,
    )
    task.save()
    chat_llm(task, data_to_analyze, tool_key="analyze_risks")
    log("response", task.response)
    llm_response = json.loads(get_json_from(task.response))

    task.structured_data = {
        "response": llm_response,
        "previous_sources": new_previous_ids,
        "previous_files": new_previous_files,
        "data_type": data_type,
        "data_id": data_id,
        "next_data_type": llm_response.get("next_data_type"),
        "next_data_id": llm_response.get("next_data_id"),
        "files": files,
    }

    analysis_so_far = "" if analysis_so_far == "No analysis yet" else analysis_so_far
    assessment = ""
    if data_to_analyze:
        assessment = "\n\n---\n\nAnalysis of %s:\n\n" % data_line
        assessment += llm_response.get("data_assessment", "")
    task.response = analysis_so_far + assessment
    task.mark_complete()

    next_iteration = 1
    if "Iteration" in task.name:
        next_iteration = 1 + int(task.name.split("Iteration")[1].strip())

    go_on = (
        next_iteration <= max_iterations
        and llm_response.get("next_data_type") != "none"
    )
    if go_on and llm_response.get("next_data_id"):
        return create_subsequent_task(task, iteration=next_iteration)
    else:
        report_prompt = get_prompt_from_github("risk-assessment")
        report_prompt = report_prompt % (
            today,
            "\n- ".join([task_to_line_for_llm(t) for t in previous_tasks]),
        )
        rating_task = create_subsequent_task(task)
        rating_task.url = AGENT_REPORT_URL
        rating_task.prompt = report_prompt
        rating_task.name = rating_task.name.split(" Iteration")[0] + " Agent Rating"
        rating_task.flags["tool_key"] = "assess_risks"
        rating_task.save()
        return rating_task


def assess_prs(task):
    if task.is_test():
        task.response = '{"rating":4, "rationale":"Test"}'
        task.structured_data["llm_ratings"] = [json.loads(get_json_from(task.response))]
        return task

    fetch = task.parent
    if not fetch or not fetch.structured_data:
        raise Exception("PR data", fetch, "not found for PR rating task %s" % task)

    to_assess = fetch.structured_data.get("open", [])
    max_days = task.cadence_days()
    cutoff = (datetime.datetime.now() - timedelta(days=max_days)).isoformat()
    for pr in fetch.structured_data.get("closed", []):
        updated = pr.get("updated_at")
        if task.flags.get("assess_all") == "true" or updated and updated > cutoff:
            to_assess.append(pr)

    prs = fetch.response.split("<!--DAI4-->")[1:]
    prs = [p.strip().rsplit("### Closed pull requests:")[0] for p in prs]

    # this is more brittle than I'd like, but it works for now
    prefixes = ["#### PR #%s" % p.get("number") for p in to_assess]
    log("PRs to assess", prefixes)
    pr_data = []
    for pr in prs:
        for pre in prefixes:
            if pr.startswith(pre):
                number = int(pre.split("#")[-1].strip())
                pr_data.append((number, pr))
                break

    # note that the issues must be in this task's depends_on_urls to ensure it's populated
    issue_task = (
        task.mission.task_set.filter(category=TaskCategory.API)
        .filter(url__endswith="/issues")
        .first()
    )
    issue_data = issue_task.response if issue_task else ""

    prs = []
    ratings = []
    for number, pr in pr_data[:MAX_PR_RATINGS]:
        log("Assessing PR", number)
        diff = get_pr_diff_by_number(task, number)
        diff = "Diff not available" if not diff else diff
        issue_key = ""
        issue = "No corresponding issue found"
        issue_info = get_issue_info_for(task, pr, number, issue_data)
        confidence = issue_info.get("confidence", 0)
        if confidence > 8:
            issue_key = issue_info.get("issue_id", "")
            if issue_key:
                issue = get_issue_for(issue_key, issue_data)
        if issue_key:
            prompt = get_prompt_from_github("rate-pr-issue")
            task.prompt = prompt % (pr, issue)
        else:
            prompt = get_prompt_from_github("rate-pr")
            task.prompt = prompt % pr
        chat_llm(task, diff, tool_key="perform_rating")
        rating = json.loads(get_json_from(task.response))
        pr = [p for p in to_assess if p["number"] == number][0]
        rating["pr"] = pr
        rating["issue_info"] = issue_info
        ratings.append(rating)

    # OK, we have the ratings, now let's generate the report
    task.structured_data["llm_ratings"] = ratings
    # explicitly mark what type of rating this is if we haven't already
    task.url = task.parent.url if not task.url else task.url
    return task


def get_issue_info_for(task, pr, number, issue_data):
    if not issue_data or not pr:
        return {}
    # TODO see if we can find an issue in title / first paragraph via regex
    if not task.flags.get("id_corresponding_issue"):
        return {}
    # use gpt-4o-mini to get the Jira issue most relevant to a given block of text
    # chat_llm expects a Task, so create a temporary one
    temp = Task(
        mission=task.mission,
        name="ID issue for PR: %s" % number,
        category=TaskCategory.LLM_QUESTION,
        flags={"time_series": "false"},
        llm=GPT_4O_MINI,
    )
    temp.prompt = get_prompt_from_github("identify-issue") % pr
    chat_llm(temp, issue_data, tool_key="identify_issue")
    response = temp.response
    try:
        temp.delete()
        return json.loads(get_json_from(response))
    except Exception as e:
        log("Error parsing issue find response", e, response)
        return {"error": str(e)}


def get_issue_for(key, data):
    issues = data.split("<!--DAI4-->")[1:]
    for issue in issues:
        if key in issue.strip().splitlines()[0]:
            return issue
    return "Issue not found"
