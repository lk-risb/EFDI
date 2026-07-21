# Live managed-fabric validation

Use this runbook only on an authorized deployment host. It does not contain or
record real namespaces, addresses, tokens, or certificates.

1. Confirm `netbird status` reports the expected profile, VPN address, and
   connected management/signaling services.
2. Run the normal rebuild/update flow and confirm the router, admin database,
   admin UI, and optional step-ca services are healthy.
3. In **Certificate Authority**, confirm bounded CA, policy signer, and managed
   trust are ready. Do not apply generated ACLs while the UI reports an
   unmanaged fabric uplink.
4. Enroll a disposable authorized child, then a grandchild under that child.
   Confirm **Network** marks both signed identities verified and draws the two
   management edges.
5. Push a harmless structured configuration change to the grandchild. Confirm
   the root records each relay hop and a signed terminal status.
6. Stop the root-to-child uplink. Confirm the child WebUI, child-to-grandchild
   topology, local pub/sub, and child management remain operational.
7. Restore the uplink and confirm topology/status freshness recovers without
   re-enrollment.
8. Quarantine the disposable grandchild, apply the child ACL, and verify both
   data and control traffic are denied. Restore it and verify recovery.
9. Decommission the disposable identities and verify the irreversible
   revocation remains visible. Remove only disposable runtime material.

Record pass/fail evidence in the deployment's protected operations system, not
in this repository.
