# Security

TextSequence is a local-first MVP. The REST API and Streamable HTTP MCP
endpoint are unauthenticated and are intended for localhost development only.
Do not expose the backend to a public network.

Do not include API keys, `.env` files, user media, project JSON, rendered
outputs, or private filesystem paths in issues or pull requests. Imported media
is read from local paths and is not uploaded by TextSequence.

To report a security concern, avoid public disclosure of sensitive details and
contact the project maintainers privately. Include reproduction steps and the
affected version when safe to do so.
