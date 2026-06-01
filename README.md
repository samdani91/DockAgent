# DockAgent

**An LLM-Driven VS Code Extension for Automated Dockerfile Generation, Testing, and Flakiness Repair**

---

DockAgent is a VS Code extension that automates the full Docker environment lifecycle for any software repository. Given a GitHub project, DockAgent can automatically generate a working Dockerfile, create tests to verify the container environment is correct, and detect and repair builds that break over time — all driven by Large Language Models working in a continuous feedback loop.

The three modules can be used independently or run together as a single one-click pipeline. A built-in chat assistant (modelled after GitHub Copilot Chat) explains errors in plain language, suggests fixes inline, and answers questions about the Docker setup without leaving the editor.

---

## The Problem

Setting up Docker environments is manual, error-prone, and fragile in three distinct ways:

- **Writing the Dockerfile** requires knowing the exact base image, package manager commands, build tools, and entry points for every project. Henkel et al. found that 26% of Dockerfiles on GitHub fail to build.
- **Verifying correctness** is overlooked — a Dockerfile that builds successfully can still produce the wrong environment: wrong library version, missing binary, misconfigured entry point.
- **Builds break over time** without any change to the Dockerfile itself, due to dependency updates, deprecated packages, or base image changes. Shabani et al. found 9.81% of monitored Dockerfiles exhibit this flaky behaviour.

DockAgent addresses all three problems in a single integrated tool.

---

## Modules

| Module | What it does |
|---|---|
| **Dockerfile Generation** | Reads the repository context (README, source files, dependency manifests), uses an LLM to generate a Dockerfile, then iteratively patches it based on build errors until the build succeeds. Also optimises the final image size. |
| **Test Generation** | Analyses the built Docker image's layer structure, scores files by importance, and automatically generates Container Structure Tests (CST) to verify that files, binaries, and metadata are correctly configured in the container. |
| **Flakiness Detection & Repair** | Runs repeated builds to detect flaky behaviour, retrieves semantically similar past repairs using RAG, and uses an LLM feedback loop to generate and validate patches until the build is stable. |
| **Chat Assistant** | A VS Code sidebar panel that receives errors and reports from all three modules and responds to developer questions in natural language. |

The key architectural contribution of DockAgent is its **Unified Feedback Loop**: test failures from the test generation module and flakiness reports from the repair module are fed back as patch instructions into the generation module, creating a continuous correctness cycle that none of the reference papers implement end-to-end.

---

## Academic Foundation

DockAgent is built on top of three peer-reviewed research papers:

**[1] Toward Automated Test Generation for Dockerfiles Based on Analysis of Docker Image Layers**
Yuki Goto, Shinsuke Matsumoto, Shinji Kusumoto
*Evaluation and Assessment in Software Engineering (EASE '25), June 2025, Istanbul, Turkey*
https://doi.org/10.1145/3756681.3757020
→ Basis for the Test Generation module

**[2] Dockerfile Flakiness: Characterization and Repair**
Taha Shabani, Noor Nashid, Parsa Alian, Ali Mesbah
*IEEE/ACM International Conference on Software Engineering (ICSE), 2025*
*(FLAKIDOCK)*
→ Basis for the Flakiness Detection & Repair module

**[3] Automatic Dockerfile Generation with Large Language Models**
Lyu et al.
*IEEE/ACM International Conference on Software Engineering (ICSE), 2026*
*(DRAFT)*
https://conf.researchr.org/details/icse-2026/icse-2026-research-track/120
→ Basis for the Dockerfile Generation module

---

## Project Info

**Student:** A. M Samdani Mozumder (Roll: 1412)
**Supervisor:** Mridha Md. Nafis Fuad
**Programme:** B.Sc. in Software Engineering
**Institution:** Institute of Information Technology (IIT), University of Dhaka
**Course:** SPL-3, 2026
