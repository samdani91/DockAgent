"""Prompt templates for flakiness repair.

Follows the paper's prompt design: a natural-language task description, the
flaky Dockerfile with its build output, a chain-of-thought instruction, the
retrieved demonstrations, and — on later attempts — the repairs that already
failed, so the model does not propose them again.
"""

SYSTEM_REPAIR = (
    "You are an expert DevOps engineer repairing a flaky Dockerfile. "
    "Dockerfile flakiness is a build failure caused by the environment changing "
    "underneath an unchanged Dockerfile — a deprecated repository, a base image "
    "that moved, a vanished download URL, an unreachable server. "
    "Reason step by step, then output the complete repaired Dockerfile."
)

#: Task description + query. {demonstrations} and {feedback} may be empty.
REPAIR_USER = """\
A Dockerfile that used to build successfully now fails. The Dockerfile itself \
has not changed, so the cause is external: a dependency, base image, package \
repository or remote server has changed.

Repair it with the smallest change that fixes the root cause.

## Failing Dockerfile
```dockerfile
{dockerfile}
```

## Build error
```
{error}
```
{demonstrations}{feedback}
## What to do
1. Identify the root cause of the failure from the build output.
2. Explain, in two or three sentences, what changed in the environment.
3. Output the complete repaired Dockerfile.

Rules:
- Change only what is needed to fix the root cause. Preserve the base image, \
entry point, exposed ports and build stages unless they ARE the cause.
- Do not add comments explaining the repair; keep the file's existing comments.
- Prefer a durable fix (an archive URL, a maintained repository, a supported \
base image tag) over disabling checks or pinning to something already broken.

Put the finished Dockerfile in a single fenced block:

```dockerfile
<the complete repaired Dockerfile>
```"""

#: One retrieved (S_e, D_e, R_e) triple.
DEMONSTRATION = """\
### Example {n}: {label}
Build error:
```
{error}
```
Repair applied:
```diff
{repair}
```
"""

DEMONSTRATIONS_HEADER = """
## Similar failures that were repaired successfully
These are real repairs to comparable failures. Use them as guidance, not as \
something to copy verbatim.

{examples}"""

#: One previously-rejected repair (R_fr, D_fr).
FALSE_REPAIR = """\
### Rejected attempt {n}
This repair was tried and the build still failed:
```dockerfile
{dockerfile}
```
It failed with:
```
{error}
```
"""

FEEDBACK_HEADER = """
## Repairs that did not work
Do not propose these again, and do not propose a variation that would fail the \
same way. Try a different root cause or a different approach.

{attempts}"""
