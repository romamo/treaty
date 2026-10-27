# treaty

## Mission

treaty is a Python CLI framework that makes command-line tools safe and cheap for AI agents to call, by implementing the CLI Agent Spec

## Users

- U-1: Python developers building CLIs that AI agents call
- U-2: AI agents, and the people who build them, that discover and drive treaty-built CLIs
- U-3: Maintainers moving an existing CLI onto the CLI Agent Spec

## Goals

- G-1: Full CLI Agent Spec conformance: every check of the spec's kit passes at all three levels
- G-2: A stable 1.0 with a frozen public API, semver after it
- G-3: An agent drives a treaty CLI right from one manifest call: fewer turns and tokens than argparse or click on the benchmark
- G-4: Adoption without reading the spec: treaty init and treaty audit lead a CLI to conformance

## Non-goals

- N-1: Runtime dependencies in core
- N-2: Python below 3.14
- N-3: Exposing argparse or any parser objects in the public API
- N-4: Mounting App instances as sub-apps

## Quality bar

- Q-1: Zero runtime dependencies in core
- Q-2: Every exit writes the JSON envelope; every exit code is declared
- Q-3: ruff, strict mypy, and the tests pass; each spec requirement has a test or a conformance check
- Q-4: After 1.0, a breaking change ships only in a major version, with a changelog entry on how to migrate
