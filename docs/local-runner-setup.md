# Local GitLab Runner Setup (Mac + Minikube)

## Why SaaS runners cannot deploy to Minikube

GitLab's shared SaaS runners run inside containers on remote machines hosted
by GitLab. A Minikube cluster on your Mac is not reachable from those machines
because:

1. **Localhost binding** — Minikube's API server listens on `127.0.0.1` (e.g.
   `https://127.0.0.1:53953`). That address resolves to the runner's own
   container, not your Mac.

2. **Local certificate paths** — `~/.kube/config` references certificate files
   under `~/.minikube/` on your Mac. Those paths do not exist inside a remote
   runner container.

A **self-hosted shell-executor runner** registered on your Mac fixes both
problems: it runs kubectl directly on your machine, shares your existing
`~/.kube/config`, and reaches the Minikube API server natively.

---

## Architecture

```
git push
    │
    ▼
GitLab CI
    ├── test jobs   ──→ GitLab SaaS runners  (Linux containers, remote)
    ├── build jobs  ──→ GitLab SaaS runners
    ├── push jobs   ──→ GitLab SaaS runners  → DockerHub
    │
    └── deploy job  ──→ self-hosted runner   (your Mac, tag: local)
                            │
                            ▼
                      ansible-playbook
                            │
                            ▼
                      kubectl apply
                            │
                            ▼
                      Minikube (localhost)
```

---

## One-time setup (~5 minutes)

### 1. Install the GitLab Runner binary

```bash
brew install gitlab-runner
```

### 2. Start the runner service

```bash
brew services start gitlab-runner
```

### 3. Register the runner with your project

Go to **GitLab → your project → Settings → CI/CD → Runners → New project runner**.

- Set a tag: `local`
- Uncheck "Run untagged jobs"
- Copy the registration token shown on the page

Then register:

```bash
gitlab-runner register
```

Answer the prompts:

```
GitLab instance URL: https://gitlab.com
Registration token:  <paste token from above>
Description:         mac-local
Tags:                local
Executor:            shell
```

### 4. Verify the runner appears as online

Go to **Settings → CI/CD → Runners** — the `mac-local` runner should show a
green dot within ~30 seconds.

### 5. Confirm prerequisites are in PATH

The shell executor runs as your user, so anything available in your terminal
is available in CI:

```bash
which kubectl   # /usr/local/bin/kubectl or /opt/homebrew/bin/kubectl
which ansible   # /opt/homebrew/bin/ansible
which minikube  # /usr/local/bin/minikube

# Install ansible if missing
brew install ansible
```

### 6. Start Minikube (if not already running)

```bash
minikube start
kubectl cluster-info   # should print the local API server address
```

---

## Required CI variables

Set these under **Settings → CI/CD → Variables**:

| Variable | Value | Options |
|---|---|---|
| `DOCKER_USERNAME` | Your DockerHub username | — |
| `DOCKER_PASSWORD` | DockerHub password or access token | **Mask** this |

The deploy job does **not** require `KUBECONFIG_CONTENT` — the shell executor
uses `~/.kube/config` on your Mac directly.

---

## Triggering a deploy

1. Push a commit to `main` — the test/build/push stages run automatically on
   SaaS runners.
2. When push completes, go to **CI/CD → Pipelines → your pipeline**.
3. Click the ▶ button next to the `deploy` job to trigger it manually.
4. The job runs on your Mac, applies the k8s manifests, and waits for rollouts.

---

## Troubleshooting

**`minikube status` shows Stopped**
```bash
minikube start
```

**`kubectl cluster-info` shows a connection refused error**
```bash
minikube status
kubectl config use-context minikube
```

**Ansible not found in CI job**
The shell executor inherits your login PATH but not your interactive shell
profile. Add ansible's path explicitly:
```bash
# In ~/.bash_profile or ~/.zprofile
export PATH="/opt/homebrew/bin:$PATH"
```
Then restart the runner: `brew services restart gitlab-runner`

**Runner shows offline in GitLab**
```bash
brew services restart gitlab-runner
gitlab-runner verify
```

**Job picks up by SaaS runner instead of local**
Confirm the deploy job has `tags: [local]` in `.gitlab-ci.yml` and that the
runner was registered with that exact tag (case-sensitive).
