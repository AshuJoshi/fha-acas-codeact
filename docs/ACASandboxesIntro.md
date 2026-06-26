# Azure Sandboxes (Preview)

[Azure Container Apps Sandboxes](https://sandboxes.azure.com/docs/sandboxes/) is a first-class resource type in Azure Container Apps that provides fast, secure, ephemeral compute environments with built-in suspend and resume capabilities. Sandboxes join the Container Apps family alongside Apps, Jobs, and Dynamic Sessions as a foundational building block for the next generation of cloud workloads.

## What Are ACA Sandboxes?

A Sandbox is a secure, isolated compute environment that can be created, used, suspended, and resumed on demand. Sandboxes are organized under **Sandbox Groups** (`Microsoft.App/SandboxGroups`), which let you manage collections of sandboxes with shared configuration.

**Key capabilities:**

- **Sub-second startup** — provisioned from prewarmed pools for near-instant availability
- **Strong isolation** — each sandbox runs in its own secure boundary, enterprise-grade security for untrusted code
- **Scale to zero** — pay nothing when idle, resources consumed only while actively running
- **Massive scale-out** — burst to thousands of concurrent sandboxes without manual intervention
- **OCI container images** — bring your own container image with your preferred runtime and tools
- **Snapshots** — suspend a sandbox capturing full memory and disk state, resume later in sub-second time

## Use Cases

| Scenario | How Sandboxes Help |
|----------|-------------------|
| AI code execution | Safely run LLM-generated code in isolated environments with instant startup |
| Development environments | On-demand, suspendable dev environments that preserve state across sessions |
| SaaS platforms | Isolated, per-tenant environments that start instantly and suspend when idle |
| Agent workflows | Persistent, isolated workspaces for AI agents that survive across task boundaries |
| CI/CD pipelines | Ephemeral build and test environments that scale to zero when idle |
| Burst workloads | Scale from zero to thousands of sandboxes in response to demand |