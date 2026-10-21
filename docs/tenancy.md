<!-- Modified for Norma. -->
# Workspace isolation and hostnames

An Agent Key identifies a workspace. The platform authenticates the key and derives that workspace's namespace on the server. The client does not supply a trusted tenant identifier.

Customer keys can be limited to a set of registered agent names. Requests must remain inside that scope. An agent name is an identifier within the authenticated workspace, not a customer selector. Changing the request URL or agent name must never grant access to another workspace.

`NORMA_RUNTIME_URL` selects the hosted gateway endpoint. It does not select or override the workspace attached to `NORMA_AGENT_KEY`. Use only a URL supplied by your workspace owner; credentials are transmitted to that endpoint.

## Customer-specific hostnames

Routing a customer hostname to the service is distinct from enforcing its customer identity. Enforcing “this hostname belongs to this key's workspace” requires both:

- A trusted edge or gateway mapping from the request hostname to the intended workspace.
- A server-side comparison of that mapped workspace with the workspace authenticated by the key, before forwarding or admitting the operation.

Hostname-to-workspace enforcement belongs to the hosted gateway and its runtime authorizer. A compatible deployment validates the direct request authority against an operator-managed workspace binding and carries a signed, key-bound tenant assertion to the runtime authorizer. Unknown, disabled, or mismatched endpoints fail closed; forwarded headers supplied by callers cannot select a workspace.

The SDK does not provision DNS, TLS, domains, or server bindings. Obtain the endpoint from your workspace owner and qualify the deployed gateway before production use. The key-derived workspace boundary remains in force regardless of hostname. A server may support explicit loopback development configuration; that is not proof of production tenant isolation.
