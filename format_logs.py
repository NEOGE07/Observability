#!/usr/bin/env python3

import re
import subprocess
import sys
from collections import defaultdict, deque

# ============================================================
# Configuration
# ============================================================

COMMAND = [
    "docker", "compose", "logs",
    "--follow",
    "--since=10m",
    "--no-color",
    "--no-log-prefix",
    "otel-agent",
]

# Recognise both trace exporters and both span-metrics exporters.
COMPONENT_PATTERN = re.compile(
    r'"otelcol\.component\.id"\s*:\s*"debug/'
    r'(before|after|metrics-before|metrics-after|metrics|logs)"'
)

TRACE_ID_PATTERN = re.compile(
    r"Trace ID\s*:\s*([0-9a-fA-F]+)"
)

TRACE_FIELDS = re.compile(
    r"Trace ID|^\s*ID\s*:|^\s*Name\s*:|^\s*Kind\s*:|"
    r"Start time|End time|Status|service\.name|"
    r"test\.|http\.|url\.path|http\.route",
    re.IGNORECASE,
)

METRIC_FIELDS = re.compile(
    r"Metric #|Name:|Description:|Unit:|DataType:|"
    r"IsMonotonic:|AggregationTemporality:|"
    r"NumberDataPoints|HistogramDataPoints|"
    r"Data point attributes:|"
    r"StartTimestamp:|Timestamp:|"
    r"Count:|Sum:|Value:|Min:|Max:|"
    r"service\.name|span\.name|span\.kind|status\.code|"
    r"collector\.instance\.id|traces\.span\.metrics",
    re.IGNORECASE,
)

HISTOGRAM_NOISE = re.compile(
    r"ExplicitBounds|BucketCounts|Explicit bounds|"
    r"^\s*Buckets #|^\s*Bucket counts:",
    re.IGNORECASE,
)

LOG_FIELDS = re.compile(
    r"ResourceLogs|ScopeLogs|LogRecord|SeverityText|"
    r"SeverityNumber|Body:|Timestamp:|service\.name",
    re.IGNORECASE,
)

ERROR_PATTERN = re.compile(
    r"\b(error|failed|refused|unavailable|no such host)\b",
    re.IGNORECASE,
)

# ============================================================
# State
# ============================================================

# Hold trace blocks until the matching before/after block arrives.
pending = {
    "before": defaultdict(deque),
    "after": defaultdict(deque),
}

# Latest complete metric output from each span-metrics pipeline.
latest_metrics = {
    "metrics-before": None,
    "metrics-after": None,
}

# Last normalised snapshots prevent repeated identical exports.
last_metric_snapshot = {
    "metrics-before": None,
    "metrics-after": None,
}

# An HTTP request is considered detected when a matching trace pair
# has been received from the before/after trace exporters.
metric_comparison_pending = False

# Track which metric pipelines have changed since the request trigger.
metric_stages_updated = set()

current_block = None


# ============================================================
# Identify telemetry output
# ============================================================

def get_component(line):
    match = COMPONENT_PATTERN.search(line)
    return match.group(1) if match else None


def get_signal(line):
    """Identify the signal in a Collector debug-exporter summary."""
    if '"resource spans"' in line:
        return "traces"

    if '"resource metrics"' in line:
        return "metrics"

    if '"resource logs"' in line:
        return "logs"

    return None


def get_trace_ids(lines):
    text = "".join(lines)
    return tuple(sorted(set(TRACE_ID_PATTERN.findall(text))))


# ============================================================
# Trace comparison
# ============================================================

def print_trace(stage, lines, trace_ids):
    print()
    print("=" * 72)
    print(f"TRACE: {stage.upper()} TRANSFORMATION")
    print("Trace ID(s): " + (", ".join(trace_ids) or "not found"))
    print("-" * 72)

    printed = set()

    for line in lines:
        if TRACE_FIELDS.search(line):
            value = line.rstrip()

            if value not in printed:
                print(value)
                printed.add(value)

    if stage == "before":
        print("Expected: test.* attributes not yet added.")
    else:
        print(
            "Check for test.processed, "
            "test.environment, and test.source."
        )

    print("=" * 72)


def print_comparison(before, after, trace_ids):
    print()
    print("#" * 72)
    print("BEFORE / AFTER TRACE COMPARISON")
    print("#" * 72)

    print_trace("before", before, trace_ids)
    print_trace("after", after, trace_ids)

    sys.stdout.flush()


# ============================================================
# Metrics parsing
# ============================================================

def normalize_metric_block(lines):
    """
    Remove volatile timestamps, connector instance IDs, and exporter
    metadata so that repeated flushes do not look like data changes.
    Keep metric values, counts, sums, and dimensions.
    """
    normalized = []

    for line in lines:
        if re.search(
            r"\b(StartTimestamp|Timestamp)\s*:",
            line,
            re.IGNORECASE,
        ):
            continue

        if "collector.instance.id:" in line:
            continue

        if '"otelcol.component.id"' in line:
            continue

        normalized.append(line.rstrip())

    return "\n".join(normalized).strip()


def extract_metric_sections(lines):
    """
    Split a debug exporter block into sections, one per metric name.
    """
    sections = {}
    current_name = None
    current_lines = []

    def save_current():
        if current_name:
            sections[current_name] = list(current_lines)

    for line in lines:
        match = re.search(r"Name:\s*([^\s]+)", line)

        if match:
            save_current()
            current_name = match.group(1)
            current_lines = [line]
        elif current_name is not None:
            current_lines.append(line)

    save_current()

    # Only include span metrics produced by the span_metrics connector.
    return {
        name: section
        for name, section in sections.items()
        if name.startswith("traces.span.metrics.")
    }


def print_metric_section(name, lines):
    print(f"\n  {name}")

    for line in lines:
        # Keep output compact by omitting histogram bucket details.
        if HISTOGRAM_NOISE.search(line):
            continue

        if METRIC_FIELDS.search(line):
            print("    " + line.strip())


def print_metrics_comparison():
    before = latest_metrics["metrics-before"]
    after = latest_metrics["metrics-after"]

    print()
    print("=" * 72)
    print("HTTP REQUEST — METRICS BEFORE / AFTER COMPARISON")
    print("=" * 72)

    # --------------------------------------------------------
    # Before transformation
    # --------------------------------------------------------

    print("\nMETRICS: BEFORE TRANSFORMATION")

    if before is None:
        print("  No before-transformation snapshot available.")
    else:
        before_sections = extract_metric_sections(before)

        if not before_sections:
            print("  No span metrics found in this snapshot.")

        for name, section in before_sections.items():
            print_metric_section(name, section)

    # --------------------------------------------------------
    # After transformation
    # --------------------------------------------------------

    print("\nMETRICS: AFTER TRANSFORMATION")

    if after is None:
        print("  No after-transformation snapshot available.")
    else:
        after_sections = extract_metric_sections(after)

        if not after_sections:
            print("  No span metrics found in this snapshot.")

        for name, section in after_sections.items():
            print_metric_section(name, section)

    # --------------------------------------------------------
    # Compare the latest metric snapshots
    # --------------------------------------------------------

    if before is not None and after is not None:
        before_sections = extract_metric_sections(before)
        after_sections = extract_metric_sections(after)

        before_normalized = {
            name: normalize_metric_block(section)
            for name, section in before_sections.items()
        }

        after_normalized = {
            name: normalize_metric_block(section)
            for name, section in after_sections.items()
        }

        print("\nMETRIC COMPARISON RESULT")

        if before_normalized == after_normalized:
            print(
                "  No differences in the displayed metric data "
                "after ignoring timestamps and connector IDs."
            )
        else:
            print("  Differences detected between the latest snapshots.")

            all_names = sorted(
                set(before_normalized) | set(after_normalized)
            )

            for name in all_names:
                before_value = before_normalized.get(name)
                after_value = after_normalized.get(name)

                if before_value is None:
                    print(
                        f"  + {name}: present only after transformation"
                    )
                elif after_value is None:
                    print(
                        f"  - {name}: present only before transformation"
                    )
                elif before_value != after_value:
                    print(f"  * {name}: metric content differs")

    print("=" * 72)
    sys.stdout.flush()


def note_http_request():
    """
    Arm the metrics comparison after a matching before/after trace pair.
    The next changed snapshot from each metrics pipeline completes it.
    """
    global metric_comparison_pending

    metric_comparison_pending = True
    metric_stages_updated.clear()


def process_metrics(component, lines):
    """
    Store updated metrics but only display them when a request has
    triggered a comparison and both pipelines have updated.
    """
    global metric_comparison_pending

    if component not in ("metrics-before", "metrics-after"):
        return

    sections = extract_metric_sections(lines)

    if not sections:
        return

    snapshot = normalize_metric_block(lines)

    # Ignore an unchanged periodic flush.
    if last_metric_snapshot[component] == snapshot:
        return

    # Record the changed snapshot.
    last_metric_snapshot[component] = snapshot
    latest_metrics[component] = list(lines)

    # Do not display metrics unless an HTTP request was detected.
    if not metric_comparison_pending:
        return

    metric_stages_updated.add(component)

    # Print once both stages have produced a changed snapshot.
    if metric_stages_updated == {
        "metrics-before",
        "metrics-after",
    }:
        print_metrics_comparison()

        metric_comparison_pending = False
        metric_stages_updated.clear()


# ============================================================
# Logs output
# ============================================================

def print_logs(lines):
    print()
    print("=" * 72)
    print("LOG RECORDS")
    print("-" * 72)

    for line in lines:
        if LOG_FIELDS.search(line):
            print(line.rstrip())

    print("=" * 72)
    sys.stdout.flush()


# ============================================================
# Process a complete telemetry block
# ============================================================

def finish_block(block):
    if not block:
        return

    stage = block["stage"]
    signal = block["signal"]
    lines = block["lines"]

    if signal == "metrics":
        process_metrics(stage, lines)
        return

    if signal == "logs":
        print_logs(lines)
        return

    if signal != "traces" or stage not in ("before", "after"):
        return

    trace_ids = get_trace_ids(lines)

    if not trace_ids:
        return

    key = trace_ids
    other_stage = "after" if stage == "before" else "before"

    if pending[other_stage][key]:
        other_lines = pending[other_stage][key].popleft()

        if stage == "before":
            print_comparison(lines, other_lines, trace_ids)
        else:
            print_comparison(other_lines, lines, trace_ids)

        # A matched trace pair indicates a processed HTTP request
        # for this instrumented Flask application.
        note_http_request()

    else:
        pending[stage][key].append(lines)


# ============================================================
# Stream Docker logs
# ============================================================

def main():
    global current_block

    print("Following Collector output from otel-agent.")
    print("Send HTTP requests to your Flask app to generate telemetry.")
    print("Metrics comparisons are printed only after a request is detected.")
    print("Press Ctrl+C to stop.\n")

    process = subprocess.Popen(
        COMMAND,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    try:
        for raw_line in process.stdout:
            line = raw_line.rstrip("\n")

            component = get_component(line)
            signal = get_signal(line)

            # A summary line marks the beginning of an exporter block.
            if component and signal:
                if current_block is not None:
                    finish_block(current_block)

                current_block = {
                    "component": component,
                    "stage": component,
                    "signal": signal,
                    "lines": [line],
                }
                continue

            if current_block is not None:
                current_block["lines"].append(line)

                # The trailing exporter metadata closes this block.
                if (
                    component == current_block["component"]
                    and signal is None
                ):
                    finish_block(current_block)
                    current_block = None

            elif ERROR_PATTERN.search(line):
                print(
                    "[COLLECTOR WARNING/ERROR] " + line.rstrip()
                )
                sys.stdout.flush()

    except KeyboardInterrupt:
        print("\nLog viewing stopped.")

    finally:
        if current_block is not None:
            finish_block(current_block)

        # Report traces that never received a matching counterpart.
        for stage in ("before", "after"):
            for trace_ids, blocks in pending[stage].items():
                while blocks:
                    print()
                    print(
                        "WARNING: Matching trace counterpart not received."
                    )
                    print("Available stage:", stage.upper())
                    print("Trace ID(s):", ", ".join(trace_ids))
                    print_trace(stage, blocks.popleft(), trace_ids)

        if process.stdout:
            process.stdout.close()

        process.terminate()

        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError:
        print(
            "Error: Docker was not found. Run this script inside Ubuntu.",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        print(
            f"Error while following Collector logs: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

#!/usr/bin/env python3

import re
import subprocess
import sys
from collections import defaultdict, deque

# ============================================================
# Configuration
# ============================================================

COMMAND = [
    "docker", "compose", "logs",
    "--follow",
    "--since=10m",
    "--no-color",
    "--no-log-prefix",
    "otel-agent",
]

COMPONENT_PATTERN = re.compile(
    r'"otelcol\.component\.id"\s*:\s*"debug/'
    r'(before|after|metrics-before|metrics-after|metrics|logs)"'
)

TRACE_ID_PATTERN = re.compile(
    r"Trace ID\s*:\s*([0-9a-fA-F]+)"
)

TRACE_FIELDS = re.compile(
    r"Trace ID|^\s*ID\s*:|^\s*Name\s*:|^\s*Kind\s*:|"
    r"Start time|End time|Status|service\.name|"
    r"test\.|http\.|url\.path|http\.route",
    re.IGNORECASE,
)

METRIC_FIELDS = re.compile(
    r"Metric #|Name:|Description:|Unit:|DataType:|"
    r"IsMonotonic:|AggregationTemporality:|"
    r"NumberDataPoints|HistogramDataPoints|"
    r"Data point attributes:|"
    r"StartTimestamp:|Timestamp:|"
    r"Count:|Sum:|Value:|Min:|Max:|"
    r"service\.name|span\.name|span\.kind|status\.code|"
    r"collector\.instance\.id|traces\.span\.metrics",
    re.IGNORECASE,
)

HISTOGRAM_NOISE = re.compile(
    r"ExplicitBounds|BucketCounts|Explicit bounds|"
    r"^\s*Buckets #|^\s*Bucket counts:",
    re.IGNORECASE,
)

LOG_FIELDS = re.compile(
    r"ResourceLogs|ScopeLogs|LogRecord|SeverityText|"
    r"SeverityNumber|Body:|Timestamp:|service\.name",
    re.IGNORECASE,
)

ERROR_PATTERN = re.compile(
    r"\b(error|failed|refused|unavailable|no such host)\b",
    re.IGNORECASE,
)

# Pair trace blocks by their Trace IDs.
pending = {
    "before": defaultdict(deque),
    "after": defaultdict(deque),
}

# Latest metric snapshots from the two independent pipelines.
latest_metrics = {
    "metrics-before": None,
    "metrics-after": None,
}

last_metric_snapshot = {
    "metrics-before": None,
    "metrics-after": None,
}

current_block = None


# ============================================================
# Identify exporter output
# ============================================================

def get_component(line):
    match = COMPONENT_PATTERN.search(line)
    return match.group(1) if match else None


def get_signal(line):
    """Identify the signal from a Collector debug-exporter summary."""
    if '"resource spans"' in line:
        return "traces"

    if '"resource metrics"' in line:
        return "metrics"

    if '"resource logs"' in line:
        return "logs"

    return None


def get_trace_ids(lines):
    text = "".join(lines)
    return tuple(sorted(set(TRACE_ID_PATTERN.findall(text))))


# ============================================================
# Trace comparison
# ============================================================

def print_trace(stage, lines, trace_ids):
    print()
    print("=" * 72)
    print(f"TRACE: {stage.upper()} TRANSFORMATION")
    print("Trace ID(s): " + (", ".join(trace_ids) or "not found"))
    print("-" * 72)

    printed = set()

    for line in lines:
        if TRACE_FIELDS.search(line):
            value = line.rstrip()

            if value not in printed:
                print(value)
                printed.add(value)

    if stage == "before":
        print("Expected: test.* attributes not yet added.")
    else:
        print("Check for test.processed, test.environment, test.source.")

    print("=" * 72)


def print_comparison(before, after, trace_ids):
    print()
    print("#" * 72)
    print("BEFORE / AFTER TRACE COMPARISON")
    print("#" * 72)

    print_trace("before", before, trace_ids)
    print_trace("after", after, trace_ids)


# ============================================================
# Metrics parsing and comparison
# ============================================================

def normalize_metric_block(lines):
    """
    Remove volatile timestamps and collector-instance IDs so that
    routine flushes do not look like meaningful metric changes.
    Preserve metric names, values, counts, sums and dimensions.
    """
    result = []

    for line in lines:
        if HISTOGRAM_NOISE.search(line):
            continue

        # Ignore timestamps that change on each export.
        if re.search(
            r"\b(StartTimestamp|Timestamp)\s*:",
            line,
            re.IGNORECASE,
        ):
            continue

        # Ignore the per-connector instance ID.
        if "collector.instance.id:" in line:
            continue

        # Ignore the Collector exporter footer metadata.
        if '"otelcol.component.id"' in line:
            continue

        result.append(line.rstrip())

    return "\n".join(result).strip()


def extract_metric_sections(lines):
    """
    Return each generated metric as a separate section.
    The section starts at its 'Name:' line and ends before the
    next metric name.
    """
    sections = {}
    current_name = None
    current_lines = []

    def save_current():
        if current_name:
            sections[current_name] = list(current_lines)

    for line in lines:
        match = re.search(r"Name:\s*([^\s]+)", line)

        if match:
            save_current()
            current_name = match.group(1)
            current_lines = [line]
        elif current_name is not None:
            current_lines.append(line)

    save_current()

    return {
        name: section
        for name, section in sections.items()
        if name.startswith("traces.span.metrics.")
    }


def print_metric_section(name, lines):
    print(f"\n  {name}")

    for line in lines:
        if HISTOGRAM_NOISE.search(line):
            continue

        if METRIC_FIELDS.search(line):
            print("    " + line.strip())


def print_metrics_comparison():
    before = latest_metrics["metrics-before"]
    after = latest_metrics["metrics-after"]

    print()
    print("=" * 72)
    print("LIVE METRICS COMPARISON")
    print("=" * 72)

    if before is None:
        print("\nBEFORE TRANSFORMATION")
        print("  Waiting for the first metrics-before export.")

    if after is None:
        print("\nAFTER TRANSFORMATION")
        print("  Waiting for the first metrics-after export.")

    if before is not None:
        print("\nMETRICS: BEFORE TRANSFORMATION")
        for name, section in extract_metric_sections(before).items():
            print_metric_section(name, section)

    if after is not None:
        print("\nMETRICS: AFTER TRANSFORMATION")
        for name, section in extract_metric_sections(after).items():
            print_metric_section(name, section)

    if before is not None and after is not None:
        before_sections = extract_metric_sections(before)
        after_sections = extract_metric_sections(after)

        before_normalized = {
            name: normalize_metric_block(section)
            for name, section in before_sections.items()
        }
        after_normalized = {
            name: normalize_metric_block(section)
            for name, section in after_sections.items()
        }

        print("\nMETRIC COMPARISON RESULT")

        if before_normalized == after_normalized:
            print(
                "  No differences in the displayed metric content "
                "after ignoring timestamps and connector instance IDs."
            )
        else:
            print("  Differences detected between the latest snapshots.")

            all_names = sorted(
                set(before_normalized) | set(after_normalized)
            )

            for name in all_names:
                b = before_normalized.get(name)
                a = after_normalized.get(name)

                if b is None:
                    print(f"  + {name}: present only after transformation")
                elif a is None:
                    print(f"  - {name}: present only before transformation")
                elif b != a:
                    print(f"  * {name}: content differs")

    print("=" * 72)
    sys.stdout.flush()


def process_metrics(component, lines):
    if component not in ("metrics-before", "metrics-after"):
        return

    sections = extract_metric_sections(lines)

    if not sections:
        return

    snapshot = normalize_metric_block(lines)

    # Suppress identical repeated flushes, but never suppress all
    # future updates just because the metric name appeared before.
    if last_metric_snapshot[component] == snapshot:
        return

    last_metric_snapshot[component] = snapshot
    latest_metrics[component] = list(lines)

    print_metrics_comparison()


# ============================================================
# Logs output
# ============================================================

def print_logs(lines):
    print()
    print("=" * 72)
    print("LOG RECORDS")
    print("-" * 72)

    for line in lines:
        if LOG_FIELDS.search(line):
            print(line.rstrip())

    print("=" * 72)


# ============================================================
# Process a completed exporter block
# ============================================================

def finish_block(block):
    if not block:
        return

    stage = block["stage"]
    signal = block["signal"]
    lines = block["lines"]

    if signal == "metrics":
        process_metrics(stage, lines)
        return

    if signal == "logs":
        print_logs(lines)
        return

    if signal != "traces" or stage not in ("before", "after"):
        return

    trace_ids = get_trace_ids(lines)

    if not trace_ids:
        return

    key = trace_ids
    other_stage = "after" if stage == "before" else "before"

    if pending[other_stage][key]:
        other_lines = pending[other_stage][key].popleft()

        if stage == "before":
            print_comparison(lines, other_lines, trace_ids)
        else:
            print_comparison(other_lines, lines, trace_ids)

        sys.stdout.flush()
    else:
        pending[stage][key].append(lines)


# ============================================================
# Stream Docker logs
# ============================================================

def main():
    global current_block

    print("Following Collector output from otel-agent.")
    print("Generate requests against your Flask app to produce telemetry.")
    print("Press Ctrl+C to stop.\n")

    process = subprocess.Popen(
        COMMAND,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    try:
        for raw_line in process.stdout:
            line = raw_line.rstrip("\n")
            component = get_component(line)

            # Collector exporter summaries indicate the beginning
            # of a new block. The component and signal can be on
            # the same JSON line.
            signal = get_signal(line)

            if component and signal:
                if current_block is not None:
                    finish_block(current_block)

                current_block = {
                    "component": component,
                    "stage": component,
                    "signal": signal,
                    "lines": [line],
                }
                continue

            if current_block is not None:
                current_block["lines"].append(line)

                # The trailing exporter metadata line closes the block.
                if (
                    component == current_block["component"]
                    and signal is None
                ):
                    finish_block(current_block)
                    current_block = None
            elif ERROR_PATTERN.search(line):
                print("[COLLECTOR WARNING/ERROR] " + line.rstrip())
                sys.stdout.flush()

    except KeyboardInterrupt:
        print("\nLog viewing stopped.")

    finally:
        if current_block is not None:
            finish_block(current_block)

        for stage in ("before", "after"):
            for trace_ids, blocks in pending[stage].items():
                while blocks:
                    print("\nWARNING: Matching trace counterpart not received.")
                    print("Available stage:", stage.upper())
                    print("Trace ID(s):", ", ".join(trace_ids))
                    print_trace(stage, blocks.popleft(), trace_ids)

        if process.stdout:
            process.stdout.close()

        process.terminate()

        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError:
        print(
            "Error: Docker was not found. Run this script inside Ubuntu.",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        print(f"Error while following Collector logs: {exc}", file=sys.stderr)
        sys.exit(1)
