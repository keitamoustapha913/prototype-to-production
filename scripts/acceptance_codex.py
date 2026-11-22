"""Drive real Codex CLI app-server turns and retain the client-visible event stream."""
import json
import os
import queue
import subprocess
import threading
import time


class Codex:
    def __init__(self, workspace, home, evidence, model, timeout):
        self.workspace = workspace
        self.model = model
        self.timeout = timeout
        self.events = queue.Queue()
        self.request_id = 0
        self.log = (evidence / "events.jsonl").open("x", buffering=1)
        self.stderr = (evidence / "codex.stderr").open("x")
        env = {**os.environ, "CODEX_HOME": str(home)}
        self.process = subprocess.Popen(
            ["codex", "app-server", "--disable", "apps", "--disable", "plugins",
             "-c", 'web_search="disabled"', "-c", "sandbox_workspace_write.network_access=true",
             "-c", 'sandbox_workspace_write.exclude_slash_tmp=true',
             "-c", 'sandbox_workspace_write.exclude_tmpdir_env_var=true'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
            text=True, cwd=workspace, env=env,
        )
        self.reader = threading.Thread(target=self.read_events, daemon=True)
        self.reader.start()

    def read_events(self):
        for line in self.process.stdout:
            self.events.put(line)
        self.events.put(None)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.reader.join(timeout=5)
        # Preserve notifications sent immediately before shutdown as well.
        while not self.events.empty():
            line = self.events.get_nowait()
            if line is not None:
                self.record("received", json.loads(line))
        self.process.stdout.close()
        self.log.close()
        self.stderr.close()

    def record(self, direction, message):
        print(json.dumps({"direction": direction, "message": message}), file=self.log)

    def send(self, message):
        self.record("sent", message)
        print(json.dumps(message), file=self.process.stdin, flush=True)

    def wait(self, predicate, observe=lambda message: None):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                line = self.events.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as error:
                raise ValueError("Timed out waiting for Codex CLI") from error
            if line is None:
                raise ValueError("Codex CLI closed its event stream")
            message = json.loads(line)
            self.record("received", message)
            if "method" in message and "id" in message:
                self.send({"id": message["id"], "error": {
                    "code": -32000, "message": "Interactive tool requests are disabled in this runner",
                }})
            observe(message)
            if predicate(message):
                return message

    def request(self, method, params, observe=lambda message: None):
        self.request_id += 1
        request_id = self.request_id
        self.send({"id": request_id, "method": method, "params": params})
        response = self.wait(lambda message: message.get("id") == request_id
                             and "method" not in message, observe)
        if "error" in response:
            raise ValueError(f"{method}: {response['error']}")
        return response["result"]

    def initialize(self):
        self.request("initialize", {"clientInfo": {"name": "p2p_acceptance", "version": "1"}})
        self.send({"method": "initialized"})

    def start(self):
        result = self.request("thread/start", {
            "cwd": str(self.workspace), "sandbox": "workspace-write",
            "approvalPolicy": "never", "model": self.model,
        })
        return {"id": result["thread"]["id"], "model": result["model"]}

    def turn(self, thread, prompt):
        displayed = []

        def observe(event):
            params = event.get("params", {})
            item = params.get("item", {})
            if (event.get("method") == "item/completed" and params.get("threadId") == thread
                    and item.get("type") == "agentMessage" and item.get("text")):
                displayed.append(item["text"])
                print(item["text"], flush=True)

        result = self.request("turn/start", {
            "threadId": thread, "input": [{"type": "text", "text": prompt}],
        }, observe)
        turn_id = result["turn"]["id"]
        completed = self.wait(lambda event: event.get("method") == "turn/completed"
                              and event["params"]["turn"]["id"] == turn_id, observe)["params"]["turn"]
        if completed["status"] != "completed":
            raise ValueError(f"Codex turn {completed['status']}: {completed.get('error')}")
        if not displayed:
            raise ValueError("Codex produced no visible response")
        return turn_id, displayed

    def compact(self, thread):
        compacted = False
        completed = None

        def observe(event):
            nonlocal compacted, completed
            params = event.get("params", {})
            if params.get("threadId") != thread:
                return
            if event.get("method") == "thread/compacted" or (
                event.get("method") == "item/completed"
                and params["item"]["type"] == "contextCompaction"
            ):
                compacted = True
            if event.get("method") == "turn/completed":
                completed = params["turn"]

        self.request("thread/compact/start", {"threadId": thread}, observe)
        if completed is None:
            self.wait(lambda _: completed is not None, observe)
        if completed["status"] != "completed" or not compacted:
            raise ValueError("Codex did not record successful context compaction")
        return completed["id"]
