# Azure DevOps Agent Dashboard

Azure DevOps Agent Dashboard is a local desktop companion for teams that want
help moving work items through their normal development process.

It gives you one place to see assigned work, review proposed plans, follow
progress, run checks, manage pull requests, and decide when the agent should
continue. You stay in control: planning and code changes have approval points,
and the dashboard records what happened along the way.

## What it helps with

- Shows Azure DevOps work assigned to you.
- Creates or reuses a local branch for each work item.
- Prepares a plan before making changes.
- Helps implement approved work and requested fixes.
- Runs project checks before a pull request is created.
- Helps review pull request comments and organize fixes.
- Keeps a clear history of decisions, checks, notifications, and feedback.

## The workflow at a glance

The **Workflow** page explains the full process with a zoomable action map.
Every action displays its tools under **Uses**, and each route has a color:

| Stage | What happens | Tools used |
|---|---|---|
| Read work | The dashboard loads assigned work and keeps its state up to date. | Azure DevOps, Git, local history |
| Understand | The agent explores the codebase and prepares a plan. | Copilot, Headroom, Graphify |
| Approve | You review and approve the plan. | Dashboard |
| Build | The approved work is implemented and prepared for review. | Claude, Git, repository tools |
| Check and share | Project checks run before the pull request is created. | Project scripts, Azure DevOps |

You can select any map card to see its purpose and leave feedback about how
the agent should behave at that point.

## How agents are used

The recommended setting is **Automatic: Copilot analysis / Claude execution**.

### Copilot, Headroom, and Graphify

Copilot handles read-only work such as planning, exploring the codebase,
breaking an Epic into smaller items, and planning pull-request fixes.

Headroom helps keep Copilot context efficient. Graphify helps it start from the
most relevant areas of a codebase when a code graph is available. If no graph is
available, planning still works through normal repository exploration.

### Claude

Claude handles work that changes the local repository: implementing approved
work, applying fixes, and Git operations such as commit and push.

Its written summaries use a compact Caveman Ultra style: important decisions,
risks, changed files, and verification results remain, while repeated filler is
removed. Structured responses such as JSON remain unchanged.

### No AI required

Several steps do not use an AI model or tokens:

- loading work items;
- creating or reusing branches;
- storing history and notifications;
- formatting and deterministic lint fixes;
- running tests, lint, type checks, and builds;
- creating and updating pull requests through Azure DevOps.

## Getting started

1. Start the desktop app.
2. Open **Settings**.
3. Enter Azure DevOps connection details, local repository path, branch, and
   access token.
4. Select **Automatic: Copilot analysis / Claude execution**.
5. Keep **Optimize agent context with Headroom** enabled.
6. Open **Ticket** and choose when to run Ingest or approve a plan.

The app stores settings and local history in your Windows profile. The access
token is never returned to the browser in plain text.

## Running the app during development

Requirements:

- Python 3.10 or later;
- Git;
- a local clone of the target Azure DevOps repository;
- Azure DevOps access token with Work Items and Code read/write permissions;
- Azure CLI, including its `azure-devops` extension;
- authenticated GitHub Copilot CLI;
- authenticated Claude Code for code-changing tasks;
- Headroom and Graphify for the recommended optimized workflow.

### Connect the command-line tools

Before running agent tasks, install and sign in to the command-line tools used
by the dashboard:

```powershell
# Azure DevOps integration
az extension add --name azure-devops

# GitHub Copilot
copilot login

# Claude tasks that change code
claude login
```

Headroom should be running and connected to Copilot. It reduces the context
sent to supported Copilot tasks and records token savings:

```powershell
headroom init -g --memory copilot
headroom proxy
```

Graphify is optional, but recommended for larger repositories. Build or update
the graph inside the target repository before asking the agent to plan work:

```powershell
graphify update .
```

Graphify improves codebase exploration by identifying relevant code paths.
Headroom improves context efficiency. Caveman Ultra improves natural-language
agent responses: it keeps decisions, risks, changed files, and checks while
removing repeated wording. These three tools improve speed, context quality,
and response clarity; they do not replace source verification or project tests.

Install Python dependencies:

```powershell
pip install -r requirements.txt
```

Start the desktop app:

```powershell
python desktop_app.py
```

The app also serves its local dashboard at `http://127.0.0.1:8765`.

For browser-only development:

```powershell
python dashboard_server.py
```

For a direct workflow run after setup:

```powershell
python ingest_loop.py
python review_loop.py
```

Do not run Ingest and Review at the same time against the same local
repository, because both use its Git working tree.

## Project layout

| File or folder | Purpose |
|---|---|
| `desktop_app.py` | Desktop window and local server startup |
| `dashboard_server.py` | Dashboard API |
| `static/` | Dashboard interface and workflow map |
| `ingest_loop.py` | Work item discovery, planning, branches, implementation |
| `review_loop.py` | Pull request comments, review, and fixes |
| `claude_runner.py` | Claude, Copilot, and Headroom routing |
| `workflow_context.py` | Graphify context and compact agent response guidance |
| `graphify_context.py` | Safe Graphify query with repository-search fallback |
| `autofix.py` | Deterministic formatting and lint fixes |
| `quality_checks.py` | Project check discovery and execution |
| `history.py` | Local history, quality results, and map feedback |
| `state.py` | Azure Boards tags and workflow state |
| `config.py` | Local dashboard settings |

## Safety defaults

- Dashboard listens only on `127.0.0.1`.
- Plans are approved before implementation begins.
- Pull requests wait for required technical checks.
- Pull-request fix batches wait for approval before commit and push.
- Graphify and Headroom help with context; agents still verify source files
  before relying on details.
