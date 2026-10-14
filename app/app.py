from flask import Flask
import time
import random

from opentelemetry import trace
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter


app = Flask(__name__)


# OpenTelemetry
provider = TracerProvider()

exporter = OTLPSpanExporter(
    endpoint="http://otel-agent:4318/v1/traces"
)

provider.add_span_processor(
    BatchSpanProcessor(exporter)
)

trace.set_tracer_provider(provider)

FlaskInstrumentor().instrument_app(app)


@app.route("/")
def home():
    return "Hello from test app!"


@app.route("/test")
def test():
    time.sleep(random.uniform(0.1, 1.0))

    return {
        "message": "test request",
        "status": "ok"
    }


@app.route("/error")
def error():
    raise Exception("Test error")


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8080
    )
