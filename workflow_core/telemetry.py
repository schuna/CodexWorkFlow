"""Observed usage totals; absent measurements never become measured zeroes."""


def usage_totals(executions, attempts=None):
    count = max(len(executions), attempts or 0)
    measured = [entry["tokens"] for entry in executions
                if type(entry.get("tokens")) is int and entry["tokens"] >= 0]
    missing = count - len(measured)
    result = {"run_count": count, "reported_run_count": len(measured), "missing_run_count": missing,
              "observed_tokens": sum(measured),
              "total_tokens": sum(measured) if count and not missing else None,
              "complete": bool(count) and not missing}
    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
        values = [(entry.get("usage") or {}).get(key) for entry in executions]
        known = [value for value in values if type(value) is int and value >= 0]
        result[key] = sum(known) if count and len(known) == count else None
        result["observed_" + key] = sum(known)
    return result


def project_usage(state):
    by_task = {task["id"]: usage_totals(task.get("executions", []), task.get("attempts", 0))
               for task in state["tasks"]}
    total = usage_totals([entry for task in state["tasks"] for entry in task.get("executions", [])],
                         sum(value["run_count"] for value in by_task.values()))
    return by_task, total
