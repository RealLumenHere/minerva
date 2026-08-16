
## Baseline close-out (2026-08-10, session claude-web-0809b)
Run 1: nephthys-qwen3-14b 4/4 (pelican_svg 1.0 @237s); qwen36-local 3/4
(pelican_svg timeout — recorded worst-case for the small model); isis-32b all
4 probes connection-reset (transient cold-load under concurrent bench
traffic; LiteLLM probe verified healthy after).
Run 2 (isis retry, solo): isis-qwen3-32b 4/4 (pelican_svg 1.0 @539s,
19 shapes/6 circles). All three local models now baselined; drift detection
armed from these runs. Timer remains DISABLED pending Frank enable.
