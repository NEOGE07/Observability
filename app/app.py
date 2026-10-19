
#!/usr/bin/env python3

import re
import subprocess
import sys
from collections import defaultdict

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

COMPOSE_SERVICE = "otel-agent"

COMPONENT_PATTERN = re.compile(
    r'"otelcol\.component\.id"\s*:\s*"debug/'
    r'(before|after|metrics-before|metrics-after|metrics|logs)"'
)

TRACE_ID_PATTERN = re.compile(
    r"Trace ID(?:\(s\))?\s*:\s*([0-9a-fA-F]+)"
)

TRACE_FIELDS = [
    re.compile(r"^\s*->\s*service\.name:"),
    re.compile(r"^\s*Trace ID\s*:"),
    re.compile(r"^\s*ID\s*:"),
    re.compile(r"^\s*Name\s*:"),
    re.compile(r"^\s*Kind\s*:"),
    re.compile(r"^\s*Start time\s*:"),
    re.compile(r"^\s*End time\s*:"),
    re.compile(r"^\s*Status code\s*:"),
    re.compile(r"^\s*Status message\s*:"),
    re.compile(r"^\s*->\s*(test\.|http\.)"),
]

METRIC_COMPONENTS = {"metrics-before", "metrics-after"}

# Most recent trace blocks, indexed by Trace ID.
pending_traces = defaultdict(dict)

# Most recent metric snapshots.
last_metric_snapshots = {
    "metrics-before": None,
    "metrics-after": None,
}

# Latest metric blocks, used to display a comparison.
latest_metric_blocks = {
    "metrics-before": None,
    "metrics-after": None,
}


# ------------------------------------------------------------
# Formatting helpers
# ------------------------------------------------------------

def extract_trace_id(lines):
    """Find the trace ID in a debug exporter block."""
    for line in lines:
        match = TRACE_ID_PATTERN.search(line)
        if match:
            return match.group(1)

    return None


def format_trace(component, lines):
    """Print a concise trace block using the original field layout."""
    trace_id = extract_trace_id(lines)

    if component == "before":
        heading = "TRACE: BEFORE TRANSFORMATION"
    else:
        heading = "TRACE: AFTER TRANSFORMATION"

    print("\n" + "=" * 68)
    print(heading)
    print("=" * 68)

    if trace_id:
        print(f"Trace ID(s): {trace_id}")

    printed = set()

    for line in lines:
        clean_line = line.rstrip()

        for pattern in TRACE_FIELDS:
            if pattern.search(clean_line):
                if clean_line not in printed:
                    print(clean_line)
                    printed.add(clean_line)
                break

    if not printed:
        print("(No recognised trace fields in this block.)")


def normalize_metric_block(lines):
    """
    Produce a comparison snapshot while ignoring timestamps and
    exporter-generated metric numbering.
    """
    normalized = []

    for line in lines:
        # Timestamp-only changes should not trigger a new comparison.
        if re.search(
            r"\b(StartTimestamp|Timestamp|Start time|End time)\s*:",
            line,
            re.IGNORECASE,
        ):
            continue

        # Metric numbering is incidental.
        line = re.sub(r"Metric #\d+", "Metric #", line)

        # Remove the leading Collector log timestamp if present.
        line = re.sub(
            r"^\s*\d{4}-\d{2}-\d{2}T"
            r"\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s+",
            "",
            line,
        )

        normalized.append(line.rstrip())

    return "\n".join(normalized).strip()


def print_metric_comparison():
    """Show the latest before/after metrics when either snapshot changes."""
    before = latest_metric_blocks["metrics-before"]
    after = latest_metric_blocks["metrics-after"]

    print("\n" + "=" * 68)
    print("METRICS COMPARISON")
    print("=" * 68)

    if before is None:
        print("\nMETRICS: BEFORE TRANSFORMATION")
        print("(Waiting for the first metric output.)")
    else:
        print("\nMETRICS: BEFORE TRANSFORMATION")
        print_metric_lines(before)

    if after is None:
        print("\nMETRICS: AFTER TRANSFORMATION")
        print("(Waiting for the first metric output.)")
    else:
        print("\nMETRICS: AFTER TRANSFORMATION")
        print_metric_lines(after)

    if before is not None and after is not None:
        before_text = normalize_metric_block(before)
        after_text = normalize_metric_block(after)

        print("\nMETRIC COMPARISON RESULT")

        if before_text == after_text:
            print(
                "No differences in the displayed metric content "
                "(timestamps excluded)."
            )
        else:
            print(
                "Metric content differs. Compare the two sections above "
                "for changed values or dimensions."
            )


def print_metric_lines(lines):
    """
    Keep the useful metric details while removing common histogram noise.
    """
    noise_patterns = [
        re.compile(r"^\s*(ExplicitBounds|BucketCounts)\s*:"),
    ]

    for line in lines:
        if any(pattern.search(line) for pattern in noise_patterns):
            continue

        # Skip an empty line or a raw JSON-only exporter header.
        if not line.strip():
            continue

        print(line.rstrip())


# ------------------------------------------------------------
# Block processing
# ------------------------------------------------------------

def process_block(component, lines):
    """Handle one debug exporter block."""
    if not lines:
        return

    if component in ("before", "after"):
        trace_id = extract_trace_id(lines)

        if not trace_id:
            return

        pending_traces[trace_id][component] = list(lines)

        pair = pending_traces[trace_id]

        # Print the comparison when the same trace has both versions.
        if "before" in pair and "after" in pair:
            format_trace("before", pair["before"])
            format_trace("after", pair["after"])

            print("\n" + "-" * 68)
            print(f"Trace comparison complete: {trace_id}")
            print("-" * 68)

            del pending_traces[trace_id]

        return

    if component in METRIC_COMPONENTS:
        snapshot = normalize_metric_block(lines)

        # Don't print a repeated snapshot if only timestamps changed.
        if last_metric_snapshots[component] == snapshot:
            return

        last_metric_snapshots[component] = snapshot
        latest_metric_blocks[component] = list(lines)

        print_metric_comparison()


def follow_logs():
    """Follow otel-agent logs and separate debug exporter output."""
    command = [
        "docker",
        "compose",
        "logs",
        "--follow",
        "--since=10m",
        "--no-color",
        "--no-log-prefix",
        COMPOSE_SERVICE,
    ]

    print(f"Following Docker Compose logs for: {COMPOSE_SERVICE}")
    print("Generate requests against your Flask app to produce telemetry.")
    print("Press Ctrl+C to stop.\n")

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    current_component = None
    current_lines = []

    try:
        for raw_line in process.stdout:
            line = raw_line.rstrip("\n")

            match = COMPONENT_PATTERN.search(line)

            if match:
                # A new exporter entry marks the end of the previous block.
                if current_component is not None and current_lines:
                    process_block(current_component, current_lines)

                current_component = match.group(1)
                current_lines = [line]
                continue

            if current_component is not None:
                current_lines.append(line)

    except KeyboardInterrupt:
        print("\nStopping log viewer...")

    finally:
        if current_component is not None and current_lines:
            process_block(current_component, current_lines)

        process.terminate()

        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

        if process.stdout:
            process.stdout.close()


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

if __name__ == "__main__":
    try:
        follow_logs()
    except FileNotFoundError:
        print(
            "Error: Docker was not found. "
            "Run this script inside your Ubuntu environment.",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        print(f"Error while following Collector logs: {exc}", file=sys.stderr)
        sys.exit(1)

