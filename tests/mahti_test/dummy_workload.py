import csv
import json
import os
import struct
import sys
import time


def main():
    print(f"Starting dummy workload (PID: {os.getpid()})...")

    output_dir = os.environ.get("OUTPUT_DIR", os.getcwd())
    run_tag = os.environ.get("RUN_TAG", "no-tag")
    project = os.environ.get("PROJECTNUM", "no-project")
    log_dir = os.path.join(output_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)

    print(f"OUTPUT_DIR  = {output_dir}")
    print(f"RUN_TAG     = {run_tag}")
    print(f"PROJECTNUM  = {project}")

    cuda_devices = os.environ.get("CUDA_VISIBLE_DEVICES", "None")
    print(f"Detected NVIDIA GPUs: {cuda_devices}")

    log_path = os.path.join(log_dir, "run.log")
    metrics_path = os.path.join(output_dir, "metrics.csv")
    model_path = os.path.join(output_dir, "model_dummy.pt")
    results_path = os.path.join(output_dir, "results.json")

    with open(log_path, "w") as logf, open(metrics_path, "w", newline="") as mf:
        writer = csv.writer(mf)
        writer.writerow(["epoch", "loss", "accuracy"])
        for i in range(1, 6):
            loss = 1.0 / i
            acc = 1.0 - loss
            line = f"Epoch {i}/5: Loss = {loss:.4f}, Acc = {acc:.4f}"
            print(line)
            logf.write(line + "\n")
            writer.writerow([i, f"{loss:.4f}", f"{acc:.4f}"])
            sys.stdout.flush()
            time.sleep(1)

    with open(model_path, "wb") as f:
        f.write(b"DUMMYMODEL\x00")
        for i in range(512):
            f.write(struct.pack("f", 0.001 * i))

    with open(results_path, "w") as f:
        json.dump({
            "status": "success",
            "final_loss": 0.2000,
            "run_tag": run_tag,
            "project": project,
            "output_dir": output_dir,
            "artifacts": {
                "metrics": metrics_path,
                "model": model_path,
                "log": log_path,
            },
        }, f, indent=2)

    print(f"Wrote: {results_path}")
    print(f"Wrote: {metrics_path}")
    print(f"Wrote: {model_path}")
    print(f"Wrote: {log_path}")
    print("Workload finished successfully!")


if __name__ == "__main__":
    main()
