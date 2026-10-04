These Linux deployment manifests come from
`kubernetes-sigs/gcp-compute-persistent-disk-csi-driver` tag `v1.20.0`,
`deploy/kubernetes/base/{controller,node_linux}`. The upstream LICENSE is included.

The local `base/kustomization.yaml` omits the upstream Windows DaemonSet.
Ansible's overlay pins the upstream release's CSI sidecar versions, switches the
driver image to the published v1.20.0 image, removes the unused snapshotter,
and replaces the key Secret with a projected Kubernetes token and WIF config.
