# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Tools the coding agent can call. Each takes typed args and returns a dict."""

import os
import signal
import subprocess

# Everything is confined to this directory. See README on why this is not a sandbox.
WORKSPACE = os.environ.get("CODING_AGENT_WORKSPACE", "./workspace")

# Wall-clock limit for a single shell command, in seconds.
TIMEOUT_SECONDS = 30

# The agent may run a command before it has written anything, so make sure the
# directory exists rather than letting subprocess fail on a missing cwd.
os.makedirs(WORKSPACE, exist_ok=True)


def _resolve(path: str) -> str:
    """Resolve a path inside the workspace, refusing anything that escapes it."""
    root = os.path.realpath(WORKSPACE)
    full = os.path.realpath(os.path.join(root, path))
    if not full.startswith(root + os.sep) and full != root:
        raise ValueError(f"path escapes workspace: {path}")
    return full


def list_files(directory: str = ".") -> dict:
    """Lists the files and directories at the given path inside the workspace."""
    try:
        target = _resolve(directory)
        return {"entries": sorted(os.listdir(target))}
    except (ValueError, OSError) as e:
        return {"error": str(e)}


def read_file(path: str) -> dict:
    """Reads a text file from the workspace and returns its contents."""
    try:
        with open(_resolve(path)) as f:
            return {"path": path, "contents": f.read()}
    except (ValueError, OSError) as e:
        return {"error": str(e)}


def write_file(path: str, contents: str) -> dict:
    """Writes text to a file in the workspace, creating or overwriting it."""
    # Validate before opening anything: opening in "w" mode truncates the
    # destination, so a bad argument would destroy the existing file and only
    # then raise. Reject non-strings up front instead.
    if not isinstance(contents, str):
        return {"error": f"contents must be a string, got {type(contents).__name__}"}
    try:
        full = _resolve(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(contents)
        return {"path": path, "bytes_written": len(contents)}
    except (ValueError, OSError) as e:
        return {"error": str(e)}


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the shell and any children it spawned, so nothing outlives the timeout."""
    if os.name == "nt":
        # /T also terminates the child processes, not just the shell.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def run_bash(command: str) -> dict:
    """Runs a shell command in the workspace and returns its output.

    On timeout the whole process group is killed. Killing only the shell would
    leave children (``python long_job.py; :``) running and still writing to the
    workspace after the tool has already reported a timeout.
    """
    # Give the command its own process group so the whole tree can be signalled.
    if os.name == "nt":
        extra = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        extra = {"start_new_session": True}
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=os.path.abspath(WORKSPACE),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **extra,
        )
    except OSError as e:
        return {"error": str(e)}
    try:
        stdout, stderr = proc.communicate(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return {"error": f"command timed out after {TIMEOUT_SECONDS}s"}
    return {
        "exit_code": proc.returncode,
        "stdout": (stdout or "")[-4000:],
        "stderr": (stderr or "")[-4000:],
    }
