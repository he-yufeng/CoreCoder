"""One stdlib-only resource server for the stdio and HTTP integration tests."""

import json
import sys


class ResourceServer:
    def __init__(self, mode="mixed"):
        self.mode = mode
        self.requests = []

    def respond(self, request):
        self.requests.append(request)
        if "id" not in request:
            return None
        method = request["method"]
        params = request.get("params", {})
        if method == "initialize":
            capabilities = {"resources": False if self.mode == "invalid-resources" else {}}
            if self.mode != "resources-only":
                capabilities["tools"] = {}
            result = {"protocolVersion": "2025-06-18", "capabilities": capabilities,
                      "serverInfo": {"name": "references", "version": "1"}}
        elif method == "tools/list" and self.mode != "resources-only":
            names = ["echo", "resources", "resources_"] if self.mode == "collision" else ["echo"]
            result = {"tools": [{"name": name, "inputSchema": {"type": "object", "properties": {}}}
                                for name in names]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "remote tool: " + params["name"]}]}
        elif method == "resources/list":
            if "cursor" not in params:
                result = {"resources": [{"name": "guide", "uri": "docs://guide", "title": "Team guide"}],
                          "nextCursor": " page:2 "}
            elif params["cursor"] == " page:2 ":
                result = {"resources": [{"name": "more", "uri": "docs://more"}]}
            elif params["cursor"] == "":
                result = {"resources": [{"name": "empty cursor", "uri": "docs://empty"}]}
            else:
                return self.error(request, -32602, "Invalid cursor")
        elif method == "resources/templates/list":
            result = {"resourceTemplates": [{"name": "chapter", "uriTemplate": "docs://guide/{chapter}"}]}
            if "cursor" not in params:
                result["nextCursor"] = " template:2 "
            elif params["cursor"] != " template:2 ":
                return self.error(request, -32602, "Invalid cursor")
        elif method == "resources/read":
            uri = params["uri"]
            if uri == "docs://missing":
                return self.error(request, -32002, "Resource not found")
            if uri == "docs://blob":
                result = {"contents": [{"uri": uri, "mimeType": "application/octet-stream", "blob": "AA=="}]}
            elif uri == "docs://broken":
                result = {"contents": "not an array"}
            elif uri == "docs://oversize":
                result = {"contents": [{"uri": uri, "text": "x" * 40000}]}
            else:
                result = {"contents": [{"uri": uri, "mimeType": "text/plain", "text": "server-owned: " + uri},
                                       {"uri": uri + "#note", "text": "项目上下文"}]}
        else:
            return self.error(request, -32601, "No such method: " + method)
        result["_meta"] = {"method": method, "params": params}
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}

    @staticmethod
    def error(request, code, message):
        return {"jsonrpc": "2.0", "id": request["id"], "error": {"code": code, "message": message}}


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    server = ResourceServer(sys.argv[1])
    for line in sys.stdin:
        response = server.respond(json.loads(line))
        if response is not None:
            print(json.dumps(response), flush=True)
