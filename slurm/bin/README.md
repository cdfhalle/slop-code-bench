# `slurm/bin/slirp4netns`

`slirp4netns` is not installed on the cluster. Jobs that put a container in
its own network namespace (loopback only, so a problem can bind ports without
seeing the host's network) need a static binary at:

```text
slurm/bin/slirp4netns
```

`config.sh` points `SCB_SLIRP` at that path. The file must be executable.
Do not commit the binary. This checkout does not download it.
