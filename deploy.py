#!/usr/bin/env python3
"""deploy.py — Deploy NetCortex MCP services to local Kubernetes.

Prerequisites:
  - minikube or k3s or Docker Desktop K8s running
  - kubectl configured
  - docker available

Usage:
  python deploy.py                # Full deploy (build + apply)
  python deploy.py --no-build     # Apply manifests only (images already built)
  python deploy.py --teardown     # Remove all NetCortex resources
  python deploy.py --status       # Show pod/service status
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path


def run(cmd: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a shell command and print it."""
    print(f"  $ {cmd}")
    result = subprocess.run(
        cmd, shell=True, capture_output=capture, text=True,
    )
    if check and result.returncode != 0:
        if capture:
            print(f"  STDERR: {result.stderr.strip()}")
        sys.exit(1)
    return result


def check_prereqs() -> None:
    """Verify kubectl and docker are available."""
    result = subprocess.run("kubectl version --client", shell=True, capture_output=True)
    if result.returncode != 0:
        print("ERROR: 'kubectl' not found or not working. Install it first.")
        sys.exit(1)
    # Plain `docker version` (no --client) works for both real Docker and
    # podman's docker-CLI-compat shim, which rejects the --client flag.
    result = subprocess.run("docker version", shell=True, capture_output=True)
    if result.returncode != 0:
        print("ERROR: 'docker' not found or not working. Install it first.")
        sys.exit(1)
    print("[OK] kubectl and docker available.")


def detect_cluster() -> str:
    """Detect which K8s environment is active and return external IP."""
    # Check minikube
    result = subprocess.run("minikube status --format={{.Host}}", shell=True, capture_output=True, text=True)
    if result.returncode == 0 and "Running" in result.stdout:
        print("[Info] Minikube detected.")
        # Point docker to minikube's daemon
        eval_cmd = subprocess.run("minikube docker-env --shell bash", shell=True, capture_output=True, text=True)
        if eval_cmd.returncode == 0:
            for line in eval_cmd.stdout.splitlines():
                if line.startswith("export "):
                    parts = line.replace("export ", "").split("=", 1)
                    if len(parts) == 2:
                        import os
                        os.environ[parts[0]] = parts[1].strip('"')
        ip_result = subprocess.run("minikube ip", shell=True, capture_output=True, text=True)
        return ip_result.stdout.strip() if ip_result.returncode == 0 else "localhost"

    # Fallback: Docker Desktop or k3s
    print("[Info] Using local Docker daemon (Docker Desktop / k3s assumed).")
    return "localhost"


def build_images() -> None:
    """Build Docker images for both services."""
    print("\n[1/5] Building Docker images...")
    run("docker build -t netcortex-telemetry:latest -f Dockerfile.telemetry .")
    run("docker build -t netcortex-rca:latest -f Dockerfile.rca .")

    # k3s uses containerd image store, which is separate from Docker/Podman.
    # Import freshly built images so kubelet can use them with imagePullPolicy=IfNotPresent.
    if shutil.which("k3s"):
        print("[Info] k3s detected. Importing local images into k3s containerd...")
        run("docker save netcortex-telemetry:latest | sudo k3s ctr images import -")
        run("docker save netcortex-rca:latest | sudo k3s ctr images import -")
        # Podman-backed docker builds are often imported as localhost/*.
        # Tag to docker.io/library/* so kubelet resolves image refs from manifests.
        run("sudo k3s ctr images tag localhost/netcortex-telemetry:latest docker.io/library/netcortex-telemetry:latest", check=False)
        run("sudo k3s ctr images tag localhost/netcortex-rca:latest docker.io/library/netcortex-rca:latest", check=False)
        print("[OK] Images imported into k3s containerd.")

    print("[OK] Images built successfully.")


def read_env_file(path: str = "config/.env") -> dict[str, str]:
    """Parse KEY=VALUE lines from config/.env without touching os.environ."""
    env_path = Path(path)
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def provision_secret() -> None:
    """Create/update the netcortex-secrets Secret from config/.env's GEMINI_API_KEY.

    Done imperatively (kubectl create ... --dry-run=client -o yaml | kubectl apply -f -)
    instead of committing a populated k8s/secrets.yaml, so the real key never lands in
    git. Falls back to the empty tracked template if no key is available locally.
    """
    gemini_api_key = read_env_file().get("GEMINI_API_KEY", "")
    if not gemini_api_key:
        print("  [Warn] No GEMINI_API_KEY in config/.env — applying empty secret template.")
        print("         RCA synthesizer will run in deterministic-fallback mode only.")
        run("kubectl apply -f k8s/secrets.yaml")
        return
    run(
        "kubectl create secret generic netcortex-secrets -n netcortex "
        f"--from-literal=gemini-api-key={gemini_api_key} "
        "--dry-run=client -o yaml | kubectl apply -f -"
    )
    print("  [OK] netcortex-secrets provisioned from config/.env.")


def apply_manifests() -> None:
    """Apply all K8s manifests in order."""
    print("\n[2/5] Applying Kubernetes manifests...")
    run("kubectl apply -f k8s/namespace.yaml")
    provision_secret()
    for m in [
        "k8s/configmap.yaml",
        "k8s/telemetry-deployment.yaml",
        "k8s/telemetry-service.yaml",
        "k8s/rca-deployment.yaml",
        "k8s/rca-service.yaml",
    ]:
        run(f"kubectl apply -f {m}")
    print("[OK] Manifests applied.")


def wait_for_pods() -> None:
    """Wait for pods to become ready."""
    print("\n[3/5] Waiting for telemetry pod...")
    result = run(
        "kubectl -n netcortex wait --for=condition=ready pod -l app=netcortex-telemetry --timeout=60s",
        check=False,
    )
    if result.returncode != 0:
        print("  WARNING: Telemetry pod not ready within 60s")
        run("kubectl -n netcortex get pods -l app=netcortex-telemetry", check=False)
    else:
        print("  Telemetry pod ready.")

    print("\n[4/5] Waiting for RCA pod...")
    result = run(
        "kubectl -n netcortex wait --for=condition=ready pod -l app=netcortex-rca --timeout=120s",
        check=False,
    )
    if result.returncode != 0:
        print("  WARNING: RCA pod not ready within 120s")
        run("kubectl -n netcortex get pods -l app=netcortex-rca", check=False)
    else:
        print("  RCA pod ready.")


def show_status() -> None:
    """Print deployment status."""
    print("\n[5/5] Deployment status:")
    run("kubectl -n netcortex get pods", check=False)
    run("kubectl -n netcortex get services", check=False)


def teardown() -> None:
    """Remove all NetCortex K8s resources."""
    print("[Teardown] Removing NetCortex resources...")
    run("kubectl delete namespace netcortex --ignore-not-found", check=False)
    print("[Done] All NetCortex resources removed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy NetCortex MCP to local K8s")
    parser.add_argument("--no-build", action="store_true", help="Skip image build")
    parser.add_argument("--teardown", action="store_true", help="Remove all resources")
    parser.add_argument("--status", action="store_true", help="Show status only")
    args = parser.parse_args()

    print("=" * 50)
    print(" NetCortex MCP v2.0 — K8s Deployment")
    print("=" * 50)

    if args.teardown:
        teardown()
        return

    if args.status:
        show_status()
        return

    check_prereqs()
    external_ip = detect_cluster()

    if not args.no_build:
        build_images()
    else:
        print("\n[1/5] Skipping build (--no-build).")

    apply_manifests()
    wait_for_pods()
    show_status()

    rca_url = f"http://{external_ip}:30900/mcp"
    print()
    print("=" * 50)
    print(" Deployment Complete!")
    print("=" * 50)
    print()
    print(f"  MCP RCA Server: {rca_url}")
    print()
    print("  Test with MCP Inspector:")
    print("    npx @modelcontextprotocol/inspector")
    print()
    print("  View logs:")
    print("    kubectl -n netcortex logs -l app=netcortex-rca --tail=20")
    print("    kubectl -n netcortex logs -l app=netcortex-telemetry --tail=20")
    print()
    print("  Teardown:")
    print("    python deploy.py --teardown")
    print()


if __name__ == "__main__":
    main()
