# Python configuration UX proof

This example shows the Python v2 configuration shorthand for named application
parameters, the friendly `rule-controls` contract name, and the `application` /
`application_version` aliases. The serialized finish request still uses the
canonical `platform` and `platform_version` fields.

The JSON and YAML configs are equivalent. From this directory, run either:

```sh
python app.py
python app.py --format yaml
```

Both commands bootstrap Norma once, invoke the configured application entrypoint,
and send the finish check through `httpx.MockTransport`. The transport double
returns a strict canonical contract acknowledgement and checks the emitted finish
body. It uses a local placeholder key that never leaves the in-process transport;
no account, API key, or model is needed.

`input` selects the first application parameter. `params.request` selects the
bound formal parameter by name, including when the caller passes it by keyword or
uses its default. Python derives these names from the target callable's signature.
