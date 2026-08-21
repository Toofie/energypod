# Repository operating instructions

Before changing this repository, read `docs/CONTINUITY.md` in full. Then read the
documents it identifies as authoritative for the part being changed. Treat the
continuity document as the durable handoff when chat or agent context is absent.

Update `docs/CONTINUITY.md` after every material milestone, architectural
decision, evidence correction, test-review cycle, implementation review, or
change to the next-work sequence. Never record credentials or private keys.

Do not connect to, probe, command, or write to real battery hardware unless the
user explicitly authorizes that specific live operation. Development and
automated tests use fakes or the simulator. Live commissioning begins in
observe-only mode and must remain fail-closed.

Do not restore compatibility with earlier applications unless the user changes
the scope. Existing and reverse-engineered code are evidence, not requirements.

