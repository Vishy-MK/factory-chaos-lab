"""Names that make the lab readable in htop and ps.

Two mechanisms, chosen by what each process can afford to lose:

`set_name` writes /proc/<pid>/comm (15 bytes) and touches nothing else. The lab finds plant
components and fault helpers by their command line, and a component's plant by its environment, so those
processes get only a name.

`set_title` rewrites the whole command line (setproctitle). That also overwrites the process's original
environment block, so it is only for processes nothing looks up: the experiment runner, the watchdog and the
agents. Their titles carry live state, which htop rereads on every refresh.

Titles are off until an entry point calls `enable()`, so tests and library callers keep their own names. Experiment
titles need `enable(experiments=True)`, which only the dedicated runner passes: an agent's dry run drives the
same controller in the agent's own process and must not rename the agent. All of it is best effort: a failure
costs the label, never the experiment.
"""

from __future__ import annotations

import ctypes
import sys

PR_SET_NAME = 15
COMM_LIMIT = 15

_enabled = False
_experiments = False


def enable(experiments: bool = False) -> None:
    global _enabled, _experiments
    _enabled = True
    _experiments = experiments


def set_name(name: str) -> None:
    """Name the process. /proc/self is the thread group leader, so this works from any thread; prctl names only
    the calling thread, and tools run on worker threads."""
    if not sys.platform.startswith("linux"):
        return
    try:
        with open("/proc/self/comm", "w") as handle:
            handle.write(name[:COMM_LIMIT])
    except OSError:
        try:
            ctypes.CDLL(None).prctl(PR_SET_NAME, name.encode()[:COMM_LIMIT], 0, 0, 0)
        except (OSError, AttributeError):
            pass


def set_title(title: str, name: str) -> None:
    if not _enabled:
        return
    try:
        import setproctitle
    except ImportError:
        pass
    else:
        setproctitle.setproctitle(title)
    # setproctitle also truncates the title into comm; the short name reads better there.
    set_name(name)


def experiment(experiment_id: str, state: str, step: int | None = None) -> None:
    if not _experiments:
        return
    detail = f"{state} step {step}" if step else state
    set_title(f"cl:experiment {experiment_id} [{detail}]", "cl:experiment")


def agent(role: str, doing: str) -> None:
    set_title(f"cl:agent {role} | {doing}", f"cl:{role}")
