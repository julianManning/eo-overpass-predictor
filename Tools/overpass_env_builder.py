"""
EO Overpass Predictor — Environment Builder

Author: Julian Manning
Created: October 2026 (adapted from cdse-sentinel2-pipeline)
Email: julian.manning@outlook.com
LinkedIn: https://www.linkedin.com/in/julian-manning/
Description: Conda environment and Jupyter kernel automation for the Landsat / Sentinel-2 
             overpass prediction workflow.

Key Features:
- Delta-based Conda environment synchronisation from Tools/env.base.yml.
- Threaded, phase-aware progress spinner for long-running Conda operations.
- Automated Jupyter kernel registration and kernel-to-environment integrity audit.

"""

# ---------------- Core / Standard Library ----------------
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---------------- Data Handling ----------------
import yaml

# ---------------- Environment / Kernel Management ----------------
from jupyter_client.kernelspec import KernelSpecManager

# ====================================================================================================

def run_conda_command(cmd, use_spinner=True):
    
    """
    Execute a shell command with a multi-threaded progress monitor and status spinner.

    This function is designed to handle long-running Conda operations by wrapping 
    subprocess execution in a non-blocking threaded reader. It parses the 
    standard output in real-time to identify key environment management phases 
    (such as 'Solving', 'Downloading', and 'Installing'), updating a dynamic 
    terminal spinner accordingly. This provides a high-quality user experience 
    by ensuring the interface remains responsive and informative while complex 
    background dependency resolutions are underway.

    Args:
        cmd (str): The full shell command to be executed.
        use_spinner (bool, optional): If True, displays a dynamic ASCII spinner 
            and phase indicator. If False, executes silently using 
            subprocess.run. Defaults to True.

    Returns:
        tuple: A triplet containing (return_code, stdout_string, stderr_string). 
            Note that when using the spinner, stderr is merged into stdout.
    """
    
    if not use_spinner:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            check=False
        )
        return result.returncode, result.stdout, result.stderr

    spinner_chars = "⣾⣽⣻⢿⡿⣟⣯⣷"
    spin_i = 0
    phase = "Starting"

    process = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    msg_queue = queue.Queue()
    collected = []

    def stream_reader(pipe, q_out):
        """Helper to read stdout into a queue."""
        for line in pipe:
            q_out.put(line)
        pipe.close()

    threading.Thread(target=stream_reader, args=(process.stdout, msg_queue), daemon=True).start()

    while process.poll() is None or not msg_queue.empty():
        try:
            line = msg_queue.get(timeout=0.1).rstrip()
            collected.append(line)

            if "Collecting package metadata" in line:
                phase = "Metadata"
            elif "Solving environment" in line:
                phase = "Solving"
            elif "Downloading" in line:
                phase = "Downloading"
            elif "Preparing transaction" in line or "Executing transaction" in line:
                phase = "Installing"

            if line:
                print(f"\n{line}")

        except queue.Empty:
            sys.stdout.write(
                f"\r⏳ conda: {phase} {spinner_chars[spin_i % len(spinner_chars)]}"
            )
            sys.stdout.flush()
            spin_i += 1
            time.sleep(0.1)

    print("\r" + " " * 80 + "\r", end="")
    return process.returncode, "\n".join(collected), ""

# ====================================================================================================

def ensure_conda_environment(env_name: str, env_yml_path: str):
    
    """
    Ensure the existence and dependency integrity of a specific Conda environment.

    This function automates the setup and maintenance of a Python environment 
    based on a provided YAML configuration. It performs a three-stage check: first 
    verifying if the environment exists (creating it if not), then auditing 
    installed packages against the YAML specification, and finally resolving 
    any discrepancies by installing missing dependencies via 'conda-forge'. This 
    idempotent workflow ensures that processing pipelines are reproducible and 
    that all necessary libraries are present before execution begins.

    Args:
        env_name (str): The target name for the Conda environment.
        env_yml_path (str): File path to the environment.yml configuration 
            containing required dependencies.

    Returns:
        None: Manages the environment state and prints status updates.

    Raises:
        FileNotFoundError: If the specified YAML configuration file does 
            not exist.
        RuntimeError: If environment creation or package installation fails 
            during the shell execution.
    """
    
    yml_path = Path(env_yml_path)
    if not yml_path.exists():
        raise FileNotFoundError(f"YAML file not found: {yml_path}")

    code, stdout, _ = run_conda_command("conda env list", use_spinner=False)
    env_exists = any(
        line.split()[0] == env_name
        for line in stdout.splitlines()
        if line and not line.startswith("#")
    )

    if not env_exists:
        print(f"✅ Creating environment '{env_name}' from {yml_path}")
        code, out, err = run_conda_command(f'conda env create -n {env_name} -f "{yml_path}" --environment-spec environment.yml')
        if code != 0:
            raise RuntimeError(err or out or "Failed to create environment")
        return

    print(f"✅ Environment '{env_name}' already exists")
    with yml_path.open(encoding='utf-8') as yaml_file:
        env_data = yaml.safe_load(yaml_file)

    deps = [dep.split("=")[0] for dep in env_data.get("dependencies", []) if isinstance(dep, str)]
    code, stdout, err = run_conda_command(f"conda list -n {env_name}", use_spinner=False)
    if code != 0:
        raise RuntimeError(err)

    installed = {line.split()[0] for line in stdout.splitlines() if line and not line.startswith("#")}
    missing = sorted(set(deps) - installed)

    if not missing:
        print("✅ All required Conda packages are already installed")
        return

    print(f"⚠️ Installing missing packages: {missing}")
    pkgs = " ".join(missing)
    code, out, err = run_conda_command(f"conda install -n {env_name} -c conda-forge -y {pkgs}")
    if code != 0:
        raise RuntimeError(err or out)
    print("✅ Environment dependency check complete")

# ====================================================================================================

def setup_jupyter_kernel(env_name="overpass"):
    
    """
    Register a specific Conda environment as a functional Jupyter kernel.

    This function bridges the gap between a standalone Conda environment and the 
    Jupyter interactive interface. It first verifies that 'jupyterlab' is 
    available within the target environment, installing it if necessary. 
    Subsequently, it checks the global Jupyter kernelspec list; if the 
    environment is not registered, it uses 'ipykernel' to install a new kernel 
    specification. This allows users to select the environment directly from 
    the Jupyter Notebook or Lab "Kernel" menu, ensuring the correct dependencies 
    are utilized during interactive analysis.

    Args:
        env_name (str, optional): The name of the Conda environment to be 
            registered. Defaults to "overpass".

    Returns:
        None: Configures the local Jupyter environment and prints confirmation 
            of readiness.
    """
    
    # Check if jupyterlab is installed
    pkg_check = subprocess.run(
        f"conda run -n {env_name} conda list jupyterlab",
        shell=True, capture_output=True, check=False
    )

    if pkg_check.returncode != 0:
        print(f"Installing jupyterlab in {env_name}...")
        run_conda_command(f"conda install -n {env_name} -c conda-forge jupyterlab -y")

    # Check if kernel exists
    kernels = subprocess.check_output("jupyter kernelspec list", shell=True, text=True)
    if env_name not in kernels:
        run_conda_command(
            f"conda run -n {env_name} python -m ipykernel install "
            f"--user --name {env_name} --display-name \"Python 3 ({env_name})\""
        )
    print(f"✅ Overpass environment and kernel '{env_name}' are ready")

# ====================================================================================================

def run_integrity_check(env_name="overpass"):
    
    """
    Perform a diagnostic audit to verify the structural integrity of the overpass environment.

    This function validates that both the Conda environment and its corresponding 
    Jupyter kernel are correctly configured and cross-linked. It inspects the 
    Jupyter kernelspec directory to verify the existence of 'kernel.json', 
    extracts the associated Python executable path, and cross-references this 
    against the list of active Conda environments on the system disk. This audit 
    is critical for troubleshooting "Kernel Dead" or "Module Not Found" errors 
    that occur when Jupyter tries to launch an environment that has been 
    moved, deleted, or incorrectly registered.

    Args:
        env_name (str, optional): The name of the environment to validate. 
            Defaults to "overpass".

    Returns:
        bool: True if both the Jupyter kernelspec and the Conda environment 
            are found and readable; False otherwise.
    """
    
    ksm = KernelSpecManager()
    kernels = ksm.find_kernel_specs()

    if env_name not in kernels:
        print(f"❌ Kernel not found: {env_name}")
        return False

    kernel_path = Path(kernels[env_name])
    kernel_json = kernel_path / "kernel.json"

    try:
        with open(kernel_json, encoding='utf-8') as file_handle:
            spec = json.load(file_handle)
        python_exe = spec["argv"][0]
        print(f"✅ Kernel found: {env_name}\n📁 Path: {kernel_path}\n🐍 Executable: {python_exe}")
    except (OSError, json.JSONDecodeError, KeyError) as err:
        print(f"⚠️ Kernel integrity error: {err}")
        return False

    # Check conda environment exists
    envs = subprocess.check_output("conda env list", shell=True, text=True)
    if env_name not in envs:
        print(f"❌ Conda environment '{env_name}' not found on disk.")
        return False

    print(f"✅ Conda environment exists: {env_name}")
    print("🎉 Overpass environment and kernel are correctly configured!")
    return True