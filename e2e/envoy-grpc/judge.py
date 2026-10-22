"""The extension with a judge for the e2e, mounted in place of its command.

Never in the image. Steered by the agent's `x-e2e-judge` header: `deny`
refuses on the headers, `body` asks for the body and refuses a JSON one with
`"deny": true`; anything else, or none, is allowed.
"""

import sys

from proxy.core.judge import Allow, Deny, NeedsBody
from proxy.envoy_grpc import server


class _E2EJudge:
    def judge_headers(self, request):
        asked = {value for name, value in request.headers if name == "x-e2e-judge"}
        if "deny" in asked:
            return Deny()
        if "body" in asked:
            return NeedsBody()
        return Allow()

    def judge_body(self, request, body):
        refused = body.is_json and isinstance(body.json, dict) and body.json.get("deny")
        return Deny() if refused else Allow()


sys.exit(server.main(judge=_E2EJudge()))
