## Docker Compose Commands
START: `docker compose up`
STOP: `docker compose stop`
STATUS: `docker compose ps`


# Challenges Faced

- ubuntu vm network failure (3 times from wk 1 - 3). Issue: sometimes need to restart NAT service on windows. 

- need to rebuild images of containers if files are changes
- poor visibility throughout the pipeline. cumbersome to check output at different stages of the pipeline. E.g. set of commands for 1st collector, another set for 2nd collector. Not easy to tell which stage of the pipeline is not working properly... need to check logs step by step
- need config kibana to receive the diff signals. when configuring signal to metric, kibana did not use the "metrics-generic.otel-default". had to change settings inside the otel-collector to set correct conversion endpoint
