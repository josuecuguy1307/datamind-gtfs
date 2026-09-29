# ML DATAMIND GTFS — operating instructions

The visible product name is **ML DATAMIND GTFS**. Existing folders, Python packages, environment variables, database schemas, and technical identifiers retain their current names for compatibility.

## Before acting

- Identify the active work, input, results destination, region/resource coverage, phase, route, and GTFS artifact only when the operator has selected them. The UI context is descriptive, not authorization.
- Start neutral: do not infer a historic regional work, map query, phase, route, GTFS artifact, or regional resource. GTFS structural validation does not require a geographic profile.
- Read the smallest relevant module and test before changing behavior.
- Keep secrets out of source, logs, prompts, and generated artifacts. Use configured environment variables.
- Preserve GTFS source data and existing operator changes. Do not delete, migrate, publish, deploy, sync, commit, push, or contact external services without explicit operator approval.

## Runtime boundaries

- The Streamlit console is the operator interface; local and server data modes are distinct.
- `local_runner` is non-interactive prompt automation. Its configured `runner.working_directory` controls the subprocess directory.
- The AI Assistance page may open a local terminal only when its local opt-in flag is enabled and the operator has reviewed a specific scope. Its launch context is session-scoped and contains no credentials or prior chat history; it does not authenticate a provider or expand permissions.
- Regional resources (for example an OSM extract under `workspace/provinces/<region>/`) are optional. Do not move them or silently select them.
- Use test or dry-run paths where available. Report commands that could affect databases, cloud services, or production before executing them.
