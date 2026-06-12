# CASSANDRA-17411 Reproduction Evidence

**Summary:** Network partition causes write CL=ONE timeouts when using counters in Cassandra 4.
**Buggy version:** cassandra:4.0.4 (fix landed in 4.0.5 / 4.1)
**Fix control image:** cassandra:4.0.5
**Components:** Consistency/Coordination
**fixVersions:** 4.0.5, 4.1-beta1, 4.1
**Namespace:** repro-17411 | **Keyspace:** repro17411 | **Topology:** 2-node ring, RF=2 (NTS dc1:2), ephemeral storage

## Root cause (from source + Jira)
For a counter write, the coordinator forwards the mutation to a "counter leader" replica chosen by
`StorageProxy.findSuitableReplica` (4.0.x). That selection filters by `isRpcReady` + snitch proximity but
does NOT exclude endpoints the FailureDetector considers DOWN. So when a replica is partitioned/unreachable
(but still advertised as a member), the buggy coordinator keeps picking it as the counter leader and the
CL=ONE counter write times out -- even though a healthy replica exists. In Cassandra 3.x the dead node was
excluded, so traffic drained off it. The Jira reporter observed this as the driver "keeping" requests on the
partitioned node; the underlying defect is server-side counter-leader selection. CHANGES.txt 4.0.5:
"Fix counter write timeouts at ONE (CASSANDRA-17411)".

## Reproducer extracted (adapted from Jira to a minimal server-observable form)
Jira reproducer = ccm 6-node/3-DC ring + DataStax driver app.py + iptables on 127.0.0.1:7000/9042, observed as
a driver-side timeout-rate shift over minutes. Since the fix is server-side, this was reduced to a 2-node
RF=2 ring where the counter-leader replica is made unresponsive on internode (SIGSTOP) while still being a
ring member, then CL=ONE counter writes are issued at the surviving coordinator. ~50% of distinct keys are
routed to the frozen leader and time out with write_type=COUNTER. A non-counter CL=ONE write is the negative
control and does NOT time out.

## Deploy
$ kubectl create ns repro-17411
$ kubectl apply -f cass-17411.yaml   # headless svc + StatefulSet replicas=2, image cassandra:4.0.4
$ kubectl rollout status statefulset/cass -n repro-17411
  partitioned roll out complete: 2 new pods have been updated...

$ kubectl exec -n repro-17411 cass-0 -- nodetool status
  Datacenter: dc1
  UN  10.244.1.165  93.42 KiB  16  100.0%  300d0bd3-...  rack1   (cass-1, kind-worker3)
  UN  10.244.3.130  74.1  KiB  16  100.0%  cedb1eda-...  rack1   (cass-0, kind-worker)

$ kubectl exec -n repro-17411 cass-0 -- cqlsh -e "
  CREATE KEYSPACE repro17411 WITH REPLICATION={'class':'NetworkTopologyStrategy','dc1':2};
  CREATE TABLE repro17411.cntr (pk uuid PRIMARY KEY, count counter);"

## Baseline (no partition): all CL=ONE counter writes succeed
$ kubectl exec -n repro-17411 cass-0 -- bash -c '... 15x UPDATE repro17411.cntr SET count=count+1 WHERE pk=uuid() at CONSISTENCY ONE ...'
  EXIT=0
$ SELECT count(*) FROM repro17411.cntr;  ->  n = 15   (15/15 succeeded)

## Fault injection: freeze the counter-leader replica (cass-1) on internode, keep it a ring member
# SIGSTOP inside the pod failed (java is PID 1, kernel ignores STOP to PID 1). Done from the kind node instead:
$ CID=$(kubectl get pod -n repro-17411 cass-1 -o jsonpath='{.status.containerStatuses[0].containerID}' | sed 's#.*/##')
$ docker exec kind-worker3 bash -c 'pgrep CassandraDaemon -> match cgroup $CID -> HOSTPID=649658'
$ docker exec kind-worker3 kill -STOP 649658
  State:	T (stopped)        # process frozen: TCP stays open, no internode response on 7000

## BUGGY SIGNATURE (cassandra:4.0.4) -- CL=ONE counter writes to fresh keys while cass-1 frozen
$ kubectl exec -n repro-17411 cass-0 -- bash -c '20x cqlsh --request-timeout=8 -e
    "CONSISTENCY ONE; UPDATE repro17411.cntr SET count = count + 1 WHERE pk = uuid();"'

  === TIMEOUT SAMPLE ===
  <stdin>:1:WriteTimeout: Error from server: code=1100 [Coordinator node timed out waiting for replica nodes' responses] message="Operation timed out - received only 0 responses." info={'consistency': 'ONE', 'required_responses': 1, 'received_responses': 0, 'write_type': 'COUNTER'}

  RESULTS: ok=10 timeouts=10 other=0        # ~50% routed to the frozen (DN) leader -> timeout

## Confirmation that the frozen node is DOWN yet still selected, and bug is counter-specific
$ kubectl exec -n repro-17411 cass-0 -- nodetool status
  DN  10.244.1.165 ...   # cass-1 marked DOWN by failure detector
  UN  10.244.3.130 ...   # cass-0 (coordinator) up
# => coordinator KNOWS cass-1 is DN but the counter-leader path still forwards to it.

# Negative control (same frozen state): NON-counter CL=ONE write to a normal table
$ 20x "CONSISTENCY ONE; INSERT INTO repro17411.reg (pk,v) VALUES (uuid(),1);"
  REGULAR-WRITE RESULTS (cass-1 frozen): ok=20 timeouts=0     # 0 timeouts -> defect is counter-leader selection only

## A/B control on fixed image cassandra:4.0.5
(to be filled after running identical workload on 4.0.5)
