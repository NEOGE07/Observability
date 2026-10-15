
import subprocess
import re
import sys
from collections import defaultdict, deque

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

command = [
    "docker", "compose", "logs",
    "--follow",
    "--since=10m",
    "--no-color",
    "--no-log-prefix",
    "otel-agent",
]

# Match the exporter summary that starts a complete output block.
SUMMARY_PATTERN = re.compile(
    r'"otelcol\.component\.id": "debug/(before|after)"'
    r'.*"resource spans"'
)

# Match the trailing exporter metadata that ends the block.
FOOTER_PATTERN = re.compile(
    r'"otelcol\.component\.id": "debug/(before|after)"'
)

TRACE_ID_PATTERN = re.compile(
    r"Trace ID\s*:\s*([0-9a-fA-F]+)"
)


# ------------------------------------------------------------
# Start Docker log stream
# ------------------------------------------------------------

process = subprocess.Popen(
    command,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1,
)

current_block = None

# Store blocks until the matching before/after output arrives.
pending = {
    "before": defaultdict(deque),
    "after": defaultdict(deque),
}


# ------------------------------------------------------------
# Identify and format exporter blocks
# ------------------------------------------------------------

def get_stage(line):
    """Identify an exporter summary or footer."""
    match = SUMMARY_PATTERN.search(line)

    if match:
        return match.group(1), "start"

    match = FOOTER_PATTERN.search(line)

    if match:
        return match.group(1), "end"

    return None, None


def get_trace_ids(lines):
    """Return the sorted Trace IDs found in an exporter block."""
    text = "".join(lines)

    ids = TRACE_ID_PATTERN.findall(text)

    return tuple(sorted(set(ids)))


def print_block(stage, lines, trace_ids):
    """Print raw output, adding only explanatory headings."""
    print()
    print("#" + "=" * 78)

    if stage == "before":
        print("# BEFORE TRANSFORMATION")
        print("# Raw output from debug/before")

    else:
        print("# AFTER TRANSFORMATION")
        print("# Raw output from debug/after")

    if trace_ids:
        print("# Trace IDs: " + ", ".join(trace_ids))

    print("#" + "=" * 78)
    print()

    # Keep all original log lines and field values unchanged.
    sys.stdout.write("".join(lines))

    if lines and not lines[-1].endswith("\n"):
        print()

    print()
    print("#" + "-" * 78)
    print("# End of " + stage.upper() + " output")
    print("#" + "-" * 78)
    print()


def print_comparison(before, after, trace_ids):
    """Print matching before/after output together."""
    print()
    print("#" + "#" * 78)
    print("# BEFORE / AFTER COMPARISON")
    print("# Matching Trace IDs: " + ", ".join(trace_ids))
    print("#")
    print("# Compare the same spans in both sections.")
    print("# In particular, check whether test.* attributes were added.")
    print("#" + "#" * 78)

    print_block("before", before, trace_ids)
    print_block("after", after, trace_ids)


def finish_block(stage, lines):
    """Pair a completed exporter block with its counterpart."""
    trace_ids = get_trace_ids(lines)

    if not trace_ids:
        print_block(stage, lines, trace_ids)
        print("# WARNING: No Trace IDs found; this block was not paired.")
        return

    key = trace_ids

    other_stage = "after" if stage == "before" else "before"

    # If the matching output arrived first, pair them now.
    if pending[other_stage][key]:
        other_lines = pending[other_stage][key].popleft()

        if stage == "before":
            print_comparison(lines, other_lines, trace_ids)
        else:
            print_comparison(other_lines, lines, trace_ids)

    else:
        # Wait for the counterpart instead of printing a misleading
        # standalone section.
        pending[stage][key].append(lines)


# ------------------------------------------------------------
# Stream and collect complete exporter blocks
# ------------------------------------------------------------

try:
    for line in process.stdout:
        stage, marker = get_stage(line)

        if marker == "start":
            # Recover gracefully if a previous block did not have
            # its expected footer.
            if current_block is not None:
                finish_block(
                    current_block["stage"],
                    current_block["lines"],
                )

            current_block = {
                "stage": stage,
                "lines": [line],
            }
            continue

        if current_block is not None:
            current_block["lines"].append(line)

            # The trailing metadata identifies the exporter again,
            # but does not contain the "resource spans" summary.
            if (
                marker == "end"
                and stage == current_block["stage"]
            ):
                finish_block(
                    current_block["stage"],
                    current_block["lines"],
                )
                current_block = None

        else:
            # Preserve unrelated collector messages.
            sys.stdout.write(line)
            sys.stdout.flush()

except KeyboardInterrupt:
    print("\n# Log viewing stopped.")

finally:
    if current_block is not None:
        finish_block(
            current_block["stage"],
            current_block["lines"],
        )

    # Show any outputs for which a matching stage never arrived.
    for stage in ("before", "after"):
        for trace_ids, blocks in pending[stage].items():
            while blocks:
                print()
                print("# WARNING: Matching counterpart not received.")
                print("# Available stage: " + stage.upper())
                print("# Trace IDs: " + ", ".join(trace_ids))
                print_block(stage, blocks.popleft(), trace_ids)

    if process.stdout:
        process.stdout.close()

    process.terminate()
    process.wait()

