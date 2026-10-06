Build tiny-service, a small internal HTTP service with two endpoints. GET /health tells an operator the process
is alive. POST /echo sends back the JSON a developer posts, so they can check what their client really sends.

It is for our own teams, runs on a developer machine, keeps no data and has no database. Python standard library
only. No staging environment exists and none is planned.
