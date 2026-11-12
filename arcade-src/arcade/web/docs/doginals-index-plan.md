# Plan: our own index of every Doginal

Written by Claude on 2026-10-20 from a talk with the operator. Not started yet: waiting for the operator's go.

## Goal

Every arcade knows every Doginal on the Dogecoin chain, about 8 million of them:
- its id, number and content type;
- where its content sits in the chain;
- where it is now (which output holds it).

All of this is read from the chain by the arcades themselves.

## Rules the operator set (2026-10-20)

- **No outside Doginals index.** Nothing is bootstrapped from one, nothing is checked against one, and none is named in code or docs. "We don't wanna do their work for them."
- **The index is not split between arcades.** At about 1 to 3 GB, every arcade keeps all of it.
- **The mesh shares the work, not the storage.** The work is the scanning and the checking.
- **Small pieces, one block at a time.** No arcade downloads a big range at once. If an arcade goes offline, its small chunk goes to someone else.
- **No work runs in browsers or on phones.** Only arcades scan.
- **It must work with today's 3 arcades and with thousands later.** It must not rely on anyone being honest.

## The ring: each arcade holds part of the index, every part on exactly two (2026-10-20)

The operator: "One arcade should hold a portion of the index ... so that every portion of the index has two nodes so that we have a redundant copy of the complete index ... based on the number of blocks or inscriptions indexed divided by the number of arcades"; then "split the token index by token numbers ... each token is indexed on exactly 2 arcades in the same way that the ORDs are". Tokens placed by a hash of the tick (his choice).

Why: a full index is several GB (Dogecoin: 14M inscriptions at ~8% of its blocks; a test machine's files 6-8 GB each), and fetching one onto .25 stopped the live site on 2026-10-20.

- **The ring:** the arcades holding a chain's index, sorted by node id, N of them. Ords are cut into N slices by inscription number; arcade i holds slices i and i+1 (wrapping round), so each slice is on exactly two arcades and each arcade stores about 2/N of the whole.
- **Every holder still stitches the whole chain** from the shared spend notes (numbering and following moves need every block; from notes it is cheap), and keeps only the rows of its slices: `insc`/`at`/`names` for its numbers.
- **Tokens:** each DRC-20/PRC-20 tick's ledger is independent of every other's. A tick sits at hash(tick) on the same ring; the two arcades after that point hold it: its deploy, mints, transfer notes and balances. A holder follows only the transfer inscriptions of its own ticks. Consistent hashing: an arcade joining or leaving moves about 1/N of the ticks.
- **Lookups** go to one of the two holders of the slice or tick asked about (the mesh `q` frames, already used by Read only).
- **Joining, leaving, going quiet:** the ring is re-cut from the arcades that said "hi" lately. A slice that lost a holder is copied to its new second holder from the one that still has it (torrent pieces, as snapshots are now), checked as snapshots are. Until the copy is whole, that slice has one holder and the Ords panel says so.
- **Small machines** (under 8 cores, a spinning disk, the live site) hold no slice by default; they help scan.
- **Confidence still counts arcades agreeing:** two holders of a slice compare their rows' digests per chunk.
- **Admin -> Ords, made for the operator (2026-10-20: "update the admin interface to be more descriptive and helpful to the admin").** Per chain, a card instead of a dense table row, in plain words:
  - **What this arcade does for this chain**, one sentence: "Holds slices 3 and 4 of 5 (inscriptions 6.2M-12.4M) and 41,000 tokens", "Helps scan: chunk 18,240, 62 of 100 blocks", "Read only: asks arcade 8c95b13 (block 4,743,637)".
  - **How complete the index is**: chain-wide progress bars for scanned once, checked by two arcades, three or more, and stitched here, each with the number behind it and a one-line "what this means".
  - **The ring**: which arcades hold which slices, which slice has only one copy right now and why, and who is copying it.
  - **Cost here**: disk used (and its share of the free space), cores in use, blocks a second; a warning when the machine is small, the disk is spinning or free space is short ("this machine serves the live site: Help scan only").
  - **Every way of taking part is a switch, opt in / opt out** (2026-10-20), per chain: Hold a part of the index, Help scan, Share spend notes, Serve index pieces, Audit other arcades, Answer lookups for the mesh. Nothing is on unless the operator turns it on.
  - **Index percentages at the top** of each chain: indexed here (block X of Y), scanned once, checked by two.
  - **Keep what is already walked** (the operator: a test machine PEP Main index, ~50%): moving to the ring reuses an existing index as the slices that arcade holds; nothing is rebuilt or wiped by itself.
  - **A network picker of coin icons** at the top, as on the Wallet; the chosen chain opens its ring of named arcades. Mock-up: walletmock/ords.html.
  - **Choices explained in place**: each mode (Hold a part of the index, Help scan, Read only, Off) with what it costs and gives, and a recommended mode for this machine.
  - **What needs the operator's attention** first: disagreements between arcades, a slice with one copy, a stalled scan, too little disk. Each with what to do.

## Redesign (2026-10-20 evening): cover first, stitch in order, audit after

This replaces "How the work is cut", "Following" and "Trust" below where they differ. The operator's words:
- "each assigned a batch of blocks to scan so that no 2 nodes are scanning the same blocks";
- "get the entire index first and then check it second";
- "two nodes in agreement ... legitimate; three or four or more ... the confidence score can go up";
- "we start from the genesis block and just walk through the block chain a section at a time; if one of the computers is faster than others it can be assigned back to back chunks ... the next one who finishes its chunk would just be assigned the next chunk in line";
- "Anyone who wants to hold the full index can request it, and it will be served torrent style from the rest of its peers that hold it; there should be a UI element in the ORD panel".

### 1. Cover: every chunk scanned once, handed out in line

- Chunks (100 blocks) are handed out from the chain's start height (STARTS) in order. Whoever is free takes the lowest chunk nobody has scanned or holds a live lease on. A fast arcade simply takes back-to-back chunks.
- **No two arcades get the same chunk while any chunk is still unscanned.** This replaces today's "verify first" choice, which prefers chunks another arcade already said, so every chunk was scanned twice from the start.
- An arcade that goes quiet loses its lease (LEASE_SECONDS), and the chunk goes to the next free arcade, from the last height it reported done.
- A walker's own words count as coverage: helpers never take a chunk a walker has already passed.

### 2. What a chunk scan records

- **Births:** as today. Each inscription's genesis (txid, input, content type) per block, plus the chunk hash.
- **Spend notes (new):** for every transaction in the chunk, the outpoints it spends and its output values, kept compact. A later chunk can spend an inscription born in an earlier one, and the scanner can't know yet which coins carry one. The notes let the stitch work it out without reading the chain again. Estimated at roughly 1-2 GB for all of Dogecoin, spread across the arcades that scanned (to be measured).

### 3. Stitch: owners and balances, in order, from the notes

- As chunks finish in line, a stitch pass applies them in block order: births, then moves by the ord koinu first-in-first-out rule, then DRC-20/PRC-20 and name ("p":"dns") rules. Chunk N's ending state feeds chunk N+1.
- The stitch reads only the notes, never the chain, so it is fast, and no machine has to read the whole chain alone.
- Each block's moves hash is kept, as the walker does today, so two stitched copies can be compared block by block.
- **Spend notes are deleted after the stitch** (the operator: "Delete after stitch"). They are kept until every arcade that asked to hold the full index has stitched past them. An audit re-scans the chunk from the chain.

### 4. Audit: only after the whole index exists

- Once every chunk has one scan, arcades re-scan chunks someone else scanned, never their own.
- **Confidence per chunk = the number of distinct arcades (operators, see Trust) that said the same hash:**
  - 1 = found, unchecked;
  - 2 = legitimate;
  - 3, 4 or more = higher confidence.
- The Ords table shows inscriptions at each level (the Agreed column, eaa77f8, becomes a breakdown).
- A different hash is settled by more scans, as in Trust below.

### 5. Holding the full index: on request, served torrent-style

- **Ords panel, per chain:** a "Hold the full index" control. An arcade that asks for it downloads the stitched index in pieces from every peer that holds it, several at once, the way a torrent does. Each piece is checked against the agreed chunk hashes and block moves hashes.
- An arcade that doesn't hold it reads from one that does (today's Read only).
- **Holders list:** the panel shows which arcades hold each chain's full index.

### Built 2026-10-20 (branch claude/ord-cover-stitch)

- Spend notes: arcade/ord_notes.py. **Measured on real Dogecoin: about 5.6 KB a block packed, so about 10 GB for the whole chain from 4,600,000 (raw blocks: about 33 GB).** The 1-2 GB estimate above was wrong. Notes are kept only until every holder has stitched past them, so an arcade holds just the notes still waiting to be stitched; a holder building from the start downloads about 10 GB over the build.
- One core scans a chunk in 1-2.5 s (about 40-80 blocks/s); .25's single walker did 23 blocks/s.
- Proved by test: an index stitched only from another arcade's notes is table-for-table the index a walk from a Core makes.
- Snapshots: arcade/ord_snapshot.py (made only on machines with 8+ cores; 1 MiB pieces; checked against agreed chunks and a second holder's moves).
- Not yet: a no-Core holder still needs some arcade on the new version to scan a chunk an older arcade said (an older arcade keeps no notes).

### Open (to measure or decide while building)

- The real size of the spend notes on Dogecoin (sample 1,000 chunks).
- The piece size and format for torrent-style serving.
- How a walker running today (a test machine, and .25 on Dogecoin + PEP Main) hands over to the stitch.

## What is stored (per arcade, SQLite `~/.dogecoinarcade/doginals-index.sqlite`)

| table | row | about |
|---|---|---|
| `insc` | id (genesis txid + index), number, genesis height, content type, content pointer (txids of the envelope pieces, or block file + offset + length on a Core arcade), size | 60-80 B |
| `at` | the output that holds it now (txid:vout), its offset in that output, the height it moved there | 45 B |
| `chunk` | chunk number, block range, status, result hash, who scanned it, signed notes | small |
| `blockhash` | height, block hash, hash of that block's scan result, hash after following | small |

The content itself is never copied. It is read from the chain when someone asks for it, and popular pieces can be cached.

## How the work is cut

- **Range:** from the first Doginal block (early 2023) to the tip.
- **Chunk:** 100 blocks. Roughly 10,000 to 15,000 chunks for the whole range. Check the real count when building.
- **Two jobs:**
  1. **Finding** looks for newly created inscriptions. Any block can be done in any order, so this job is split across the mesh in chunks.
  2. **Following** works out where each inscription is now. This depends on everything earlier, so it is one walk in block order. Each Core arcade runs its own walk.

### Finding: the chunk loop

1. An arcade claims the next free chunk in the mesh room `doginals-scan`, the same "first taker wins" pattern as the shared order book. It gets a 10-minute lease.
2. Loop: fetch one block, check it against the header chain, scan it, write that block's result, then the next block. At most 1-2 blocks are held in memory.
3. Every 10 blocks it sends "chunk N, done to height H" to the room. This renews the lease.
4. If the lease runs out, the chunk is free again. The next taker starts from the last height reported done, so no finished block is redone.
5. At the end it publishes the chunk result:
   - the new inscriptions per block;
   - the "envelope continues in tx T" notes for Doginals that span several transactions;
   - a hash per block plus the chunk hash, signed with the arcade's node key.
6. An arcade that keeps dropping chunks is offered fewer of them.

**Where blocks come from:**
- An arcade with Dogecoin Core reads its own node. That means one block at a time, raw (`getblock <hash> 0`), with a cap on reads per second. Two reasons for the cap: .25's disk is an HDD, and many requests at once flood dogecoind's RPC queue, as happened before with Doginal reads.
- An arcade with no Core fetches raw blocks from Core arcades over the mesh. It checks each block's hash against its header chain and the transactions against the merkle root, so it trusts no one.

### Doginals that span several transactions

A Doginal's content can be split over a chain of transactions, each spending the previous one's P2SH output. A chunk can end in the middle of one. The finder records "piece k of n started in tx T, continues in the spend of T:vout". The follower walk joins the pieces, since it already follows spends. The parsing code in arcade/doginals.py (trace) is the starting point. It and the new scanner must agree on the same envelopes; a test runs both on the same transactions.

### Following: who holds what

- Each Core arcade walks forward through the blocks, starting as soon as the earliest chunks are found. It keeps the set of outputs carrying an inscription in memory (8M entries fit). On each spend it moves the inscription by the ord-dogecoin sat rules, the same rules build_doginal_send already follows.
- After every block it stores the hash of the moves made in that block and compares it with the other Core arcades over the mesh. A mismatch stops the walk at that height and is logged with both answers.
- An arcade with no Core takes the follow results from Core arcades. It accepts a block's moves only when two Core arcades run by different operators agree. It also spot-checks random blocks against blocks it fetches itself.

## Trust: how a lie or a bug gets caught

- **Each chunk is scanned twice.** While there are fewer than 5 arcades, every chunk is scanned by 2 different arcades. A chunk counts as `agreed` when both hashes match. With 3 arcades this doubles a cheap job, which is fine.
- **Later, one scan plus spot checks.** With many arcades, each chunk is scanned once, and every arcade re-scans a few random chunks (or single blocks) itself.
- **A mismatch is settled by a third scan.** The arcade that was wrong:
  - is blocked by the arcade that caught it, for the realtime mesh only;
  - has all its other chunks re-queued.
- **Operators count, not arcades.** Agreement is counted by distinct operators, not by arcade count, because running 500 fake arcades should buy nothing. Long-running arcades weigh more.
- **The money paths trust only what this arcade checked itself:**
  - wallet protection (never spend an inscribed coin);
  - the Exchange Doginals tab;
  - the send builder.

  On a Core arcade that means its own scan and its own walk, or `agreed` plus its own walk. Until then, today's per-coin chain walk (doginals.trace) stays the answer.

## Statuses an entry can have

- `found`: one arcade's scan has it.
- `agreed`: two independent scans have it with the same hash.
- `followed`: its current location comes from this arcade's own walk, or from 2 operators agreeing, for a no-Core arcade.

The Wallet and the NFTs pages can show Doginals from `found` on, labelled. Money paths need `agreed` + `followed`.

## New blocks

Once the history is done, each Core arcade:
- scans every new block itself, which is cheap;
- moves its own walk forward;
- publishes the block's two hashes.

The mesh is then only for comparing hashes and for new arcades catching up. On a reorg, it rolls back the blocks that were replaced: the scan rows, the moves and the hashes. The 2026-09-29 reorg wipe is the warning.

## A new arcade joining

1. Download the finished index from peers, in chunks.
2. Check the chunk hashes against what the mesh agreed on.
3. Spot-check some blocks itself.
4. If it has Core, run its own follow walk to confirm the current locations.
5. Then take part in new chunks like any other arcade.

## Build order

1. **Scanner, single arcade:** the block loop on .25 alone, rate-limited, writing `insc` and `blockhash`. Tests on regtest blocks with known Doginals, single- and multi-transaction, checked against doginals.trace.
2. **Follower walk:** the in-memory output set, ord-dogecoin moves, per-block hashes, reorg rollback. Tests: transfers, an inscription moved with its coin split, a reorg.
3. **Chunks over the mesh:** the `doginals-scan` room, claim/lease/progress/result, signed notes, double scans, mismatch handling. Test with 3 local arcades, one killed mid-chunk.
4. **No-Core arcades:** blocks over the mesh, header-chain and merkle checks.
5. **Use it:**
   - the Wallet's Doginals and the NFTs pages read the index;
   - the money paths switch over once `agreed` + `followed` are in place;
   - the old per-coin walk stays as the fallback until then.
6. **Joining and catching up:** download the index from peers and check it.

Steps 1-2 alone already give .25 a full index; the mesh steps spread the work and add cross-checks.

## Open questions for the operator

- How chunks are handed out while there are 3 arcades: the mesh "first taker wins" room from day one (recommended), or .25 hands them out at first?
- Which of today's 3 arcades run a full Dogecoin Core?
- Does .25 have the disk for the Dogecoin chain plus the index? It has a 500 GB HDD and also runs PEP and Litecoin. Check `du` before step 1.

## Rings A / B / C replace the slice pairs (2026-10-20, late)

The operator: "it's two complete indexes stacked on top of each other in a ring ... you only have to be assigned to the inner ring or the outer ring ... ring A or ring B ... a C level ring and added redundancy as the network goes to tens of thousands of arcades." And: a new arcade "will have a 0% sliver, and as it's allocated ... its percentage should grow on the circle"; a test machine "holds 50% of the chain on Pepe, it should show that".

- **Each ring is one whole copy** of a chain's index (ords by inscription number, tokens by tick-hash position on the same 0..1 line). Every holding arcade is on exactly ONE ring and holds ONE contiguous stretch of it.
- **Stretches are sized by what each arcade actually holds now**, not by fixed equal slices. Seams on A and B are set independently, so they fall in different places, and every point is on one arcade of each ring.
- **Joining:** a new holder goes on the ring with less held (ties: fewer members), sits right after that ring's biggest holder, and starts as a 0% sliver. It is copied in from the other ring's holders or the donor and grows to its fair share (indexed fraction / members). The donor lets go of its tail only once the copy is in, so copies never drop below 2 during a move.
- **Indexed frontier:** stretches only reach as far as the chain is indexed; past it shows as "not indexed yet". PEP Main today: ring A = @strawberry (a test machine) 49%, ring B empty -> the next PEP holder goes on ring B.
- **Quiet arcade:** its stretch shows dashed red, the copies ring shows 1 copy there, and the next arcade to come online takes that stretch over.
- **Ring C** opens when there are enough holders for a third whole copy (threshold TBD); same rules.
- **Admin -> Ords ring view:** stacked rings A (inner), B, C (dashed until open), arcs named with their %, a thin copies ring inside (green 2+, amber 1, red 0, grey not indexed), centre = % on 2 rings; a strip below with one row per ring plus copies; updates live as arcs move. Mock-up: ords.html (scratchpad, Claude session 55cfedea).

### Ring upkeep rules (2026-10-20, later still)

- **An arcade goes offline:** its stretch is a gap. The arcades either side of it on that ring stretch toward each other, copying from the other ring(s), until they meet.
- **An arcade comes online while there is a gap:** it starts at the CENTRE of the biggest gap and grows outward toward the two filling it. (One rule covers both: the arcade on each side of a gap grows into it.) With no gap, it starts beside the biggest holder of the ring with fewer arcades.
- **Once a ring has no gap,** after anyone left or joined, it re-balances: every boundary moves toward equal shares. Growing never waits; letting go happens only here, once the whole ring is covered.
- **Seams never line up across rings** (the operator: "there should never be a split seam that aligns with the split from the other ring"). Every place a seam is put (the first layout, where two arcades filling a gap meet, the even-share turn a ring rebalances to) is the place furthest from the other rings' seams (SEP 6% minimum; mock-up settles at 8%+). Ring B's stretches wrap from the end of the index back to the start.
- The thin inner ring of the view is the COPIES ring (not tokens): tokens ride inside each stretch on A and on B, so they are doubled too.

### Building the split (rings A/B) on the slim layout -- design (Claude 2026-10-21, for the operator's go-ahead)

What a holder keeps today (PEP Main, layout 2, 2.1 GB): inscriptions (rows + lookup tables ~1.1 GB), where each one is
(`at` + its lookup ~0.64 GB), blocks/notes (~0.3 GB), names/tokens (small).

**What every holder must still keep whole (cannot be split without wrong answers):**
- **Where every inscription's FIRST koinu sits** -- a new inscription is born only when no inscription already sits on
  the first koinu of input 0 (ord's reinscription rule); numbering every later inscription right needs that answer
  for every coin, so every holder keeps the set of occupied outpoints. Kept as bare 36-byte outpoints (no row ids, no
  heights): ~0.25 GB for PEP Main's 6.7M, instead of 0.64 GB.
- **The count of births** (inscription numbers are global): from the shared notes, cheap.
- **The chain walk itself** (every block's notes) -- unchanged, shared by all.

**What is split around the ring (each holder keeps only its stretch, on its ring):**
- **Inscriptions by number** -- the rows (id, type, protocol, size, height) and where each one is with its offset,
  for numbers in the stretch. Lookups by id go to the stretch's holder on either ring (the id -> number map is the
  one global piece: kept whole as 32+4 bytes each, ~0.24 GB, or answered by asking around).
- **Tokens by a hash of the tick** -- deploys, mints, balances and the transfer inscriptions of those ticks (their
  moves followed by the token's holder).
- **Names by a hash of the name** -- every claim of a name, so its first valid claim is decided in one place.

**Per holder, PEP Main with 5 arcades on each of two rings:** ~0.5 GB kept whole + ~1.4 GB / 5 = ~0.8 GB, against
2.1 GB for the whole index. Dogecoin (14M inscriptions) roughly double.

**Steps:**
1. The compact whole set (occupied outpoints, id -> number) as its own tables; the walk follows only its stretch's
   rows. Converting an existing full index = deleting what lies outside its stretch (nothing walked again).
2. Ring membership over the mesh: each holder says its ring, stretch and how far it reaches; the ring rules from the
   mock-up (gap filling from both sides, a newcomer at the centre of the biggest gap, even shares once whole, seams
   never aligned) decide stretches; a stretch grows only by COPYING rows from the other ring's holders (pieces,
   checked against the chunk hashes two arcades agreed on), and lets go only once the new holder has it.
3. Lookups routed to a stretch's holders (either ring), the Look up box and every wallet/exchange read included;
   the chain walk (doginals.trace) stays the fallback.
4. The Ords panel's rings show stretches instead of whole copies.
