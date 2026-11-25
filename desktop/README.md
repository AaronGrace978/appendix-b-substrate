# Appendix B Desktop Lab (experimental v0.4)

Requires Python 3.10+ and Node.js 22+ (npm). From the repository root:

```sh
cd desktop
npm install
npm start
```

Electron is pinned to 45.5.0, the stable release listed on Nov 24, 2026.
On Windows, set APPENDIX_B_PYTHON to a Python executable path if python is not on PATH.
The app launches the Python substrate in a child process, not a web server.

Providers: OpenAI Responses API, Anthropic Messages API, OpenRouter Chat Completions,
Ollama Cloud and Ollama Local. Enter API keys per session or use environment variables
OPENAI_API_KEY, ANTHROPIC_API_KEY, OPENROUTER_API_KEY, OLLAMA_API_KEY.
Refresh model catalog to see API-returned IDs; not every listed model supports text generation.
Local Ollama must be running on port 11434. Cloud requests send task data to the selected
provider and incur charges. No automatic fallback or secret persistence. Keys are sent to
the Python child via stdin, not command-line arguments. SQLite stores research traces and
state under Electron userData/research; do not enter sensitive tasks.

Letter-position mode requires exact ASCII letter positions and is independently checked by Python.
Open research mode records candidate claims, counterexamples and optional model critique;
its verdict stays inconclusive because model agreement is not proof.

Compare mode runs fixed then adaptive with fresh isolated state and equal candidate limits.
Actual calls, token usage and latency are displayed; equal candidate limits are not equal
token or cost budgets. Repeated randomized trials are needed for a scientific conclusion.
Persistent modes maintain separate state by provider/model/condition. Cancel terminates the
local child but cannot guarantee that remote computation or billing stops.

```sh
cd ..
python -m unittest discover -v
cd desktop
npm run check
```

## Chat

The Chat tab sends multi-turn conversations to the selected provider and model. Choose a
provider, enter a key (or use the environment variable), and the model dropdown fills from
the provider's catalog; pick **Custom model ID…** to type one. **Protocol** prepends a system
prompt that asks the model to answer as DECOMPOSE / ATTACK / VERIFY / CONCLUDE; the headings
are badged in the reply. This is a prompting aid, not verification.

## Windows release

Pushing to `main` builds a Windows x64 installer (Electron + private Python runtime), installs it
silently on the CI runner, launch-checks it, and publishes it as release `v<version>` using the
version in `desktop/package.json`. Bump that version to cut a new release. The build is
unsigned; Windows SmartScreen may warn (More info → Run anyway).

## Credits

With credit to [DinoClaw](https://github.com/AaronGrace978/DinoClaw), a fellow AI desktop project by the creator of Appendix B.

