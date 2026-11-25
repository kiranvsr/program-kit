# Playbook 12: lanes (heavy runs on other machines)

## Purpose

Set up lanes on one or more other machines for `pk remote`, so that hand-back gates, deferred plants, full suites and the release fingerprint run there instead of on the machine the agents work on. Who starts those runs is the controller's business (`kit/briefs/controller.md`, playbook 08); this playbook is the machine side, done once per machine and checked after every change to it. Nothing here needs root, and no committed file names a host, a user or an address.

## Inputs

- A machine reachable by ssh with a key and no prompt (`BatchMode`), with git, tar and Python 3.11 or later for the lane user.
- The `remote` section of `program/checks.yaml` on main (the example is in `kit/templates/checks.yaml`; `pk remote` explains each key).
- The project's toolchain lock: the versions and sha256 checksums of what its commands need, as a pins file for `pk fetch`.

## Steps

1. Hosts. Export one environment variable per machine holding its ssh destination, best an alias of your ssh config: `host_env` names it for one machine, `hosts_env: [VAR, ...]` for several, tried in that order. Only the variable's name is ever written down.
2. Mirror and lanes. On each machine, under the base directory `dir` (relative to the lane user's home): `git init --bare <dir>/repo.git`, then `git clone <dir>/repo.git <dir>/<lane>` once per lane after the first push. Each lane is its own checkout with its own `pk lock`. Give lanes sizes (`{name: lane1, size: big}`) and each kind of step its size (`sizes: {gate: big, plants: big, run: small}`); a big lane is the one with the cores and memory for a full suite.
3. Toolchain per lane user, without root. Fetch the pinned archives on the machine with `pk fetch --pins <file.json>` (each one is checked against its sha256), unpack them under the lane user's home, and put their `bin` directories first on PATH in that user's own profile or in a `setup` step. Never in `remote.env`: a PATH from another machine is wrong on this one.
4. Environment files that know their host. A project env file the tests load must not carry another machine's DOCKER_HOST, PATH or SSL_CERT_FILE onto a lane. Write the lane's own file on the lane (a `setup` step), or have the loader drop those keys when the path they name is not on this host. Secrets stay out of `remote.env`, the repository and the lane logs; a run that needs a secret or a live connection only one machine holds stays on that machine.
5. Ignored files. A lane worktree is a fresh checkout of the committed branch: whatever is git-ignored and needed (installed dependencies, generated fixtures, browser binaries) comes from a `setup` step, offline from a cache where it can, or from an extension's `prepare_tree` (purpose `remote`, with the lane's checkout as `ctx.root`; the fingerprint's tree gets purpose `fingerprint`), which links in what the lane's checkout has installed. Install dependencies from the lockfile in frozen mode, so the tree stays clean for the gate's clean-tree check. The one exception: when an install must rewrite the lockfile, the same `setup` step restores it by exact path afterwards (`git checkout -- <lockfile>`), so the gate judges the committed tree.
6. Containers. Run Docker rootless (or an equivalent) as the lane user; that user's profile sets its own DOCKER_HOST. Give each lane its own compose project name and limits: compose services take `cpuset: ${SLOT_CPUS}` and `mem_limit: ${SLOT_MEM}`, set per lane through `lane_wrap` (for example `env SLOT_CPUS=0-3 SLOT_MEM=6g`), and testcontainers get the same limits through their own resource options. Two lanes then never share a container or starve each other.
7. Shared machines (optional). A machine shared with other projects may provide its own slot command that queues heavy work across all of them, for example `slot run big -- <command>` or `slot run small -- <command>`. Put it in `lane_wrap` or `wrap`. It is the host's, not the kit's: the kit itself needs only each lane's `pk lock`.
8. A current main. Run `pk remote` from the main checkout, or from a worktree branched from current main. From 0.7.3 a push whose main is behind the mirror's is refused, because it would rewind the mirror under a queued step. Never move main in a scratch clone to get past the refusal; `--force-main` is for repairing the mirror on purpose.
9. A kit upgrade on a branch. From 0.7.4 a lane runs a step's `pk` commands with the run tree's kit when its `kit/VERSION` is newer than main's, so a kit-upgrade branch can be gated or fingerprinted on a lane; only the lane lock keeps main's kit. A branch whose kit is older than main's runs with main's kit, as before.
9. Check. `pk remote doctor` checks every machine with one ssh call each: python3, git, tar, the mirror, each lane's origin, and whether the DOCKER_HOST socket and SSL_CERT_FILE the lane's environment names are there. Then `pk remote status` (disk use, each lane's size, lock and last log lines), then one `pk remote run <owner> --size <size> -- <build command>` per lane size. Give every long run a hard limit on the lane: `pk remote run <owner> -- timeout -k 60 <seconds> <command>`.
10. Record it. `program/COMMON.md` says which variables to export, which sizes exist and which steps go to lanes (`08_execute.md`, step 6). Rerun step 9 after any change to a machine.

## Outputs

- The `remote` section on main, lanes on each machine that pass `pk remote doctor`, and the lane notes in `program/COMMON.md`.

## Done when

`pk remote doctor` ends `remote doctor: ok`, and one `pk remote gate <ID>` and one `pk remote run` per lane size ended as they do on the local machine.

## Next playbook

`08_execute.md`: the controller sends the heavy steps to the lanes from there.
