<!-- Modified for Norma. -->
# Norma with Strands

Install the `strands-agents` extra alongside the built Norma wheel. Configure
`NORMA_RUNTIME_URL` and `NORMA_AGENT_KEY`, then initialize the explicit agent:

```python
import norma
from norma.integrations.strands import NormaPlugin
from strands import Agent

norma.init(agent_name="customer-service-agent")
plugin = NormaPlugin(agent_name="customer-service-agent")
agent = Agent(model=model, tools=tools, plugins=[plugin])
```

`NormaPlugin` checks hosted Rules through framework hooks. `NormaSteeringHandler`
can translate steering decisions into framework guidance. It does not retry a
protected tool. Post-stage failures cannot undo a tool that already ran. Rules and
agent permissions are configured in Console; local evaluators are unsupported.
