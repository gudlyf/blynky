# Blynky guide

Everything the [README](../README.md) leaves out: how each part of the UI
behaves, where things are kept, and why it works the way it does.

- [Using it](#using-it)
- [How old the stills are](#how-old-the-stills-are)
- [The library](#the-library)
- [The timeline](#the-timeline)
- [What the model saw](#what-the-model-saw)
- [Using Gemini as an alternative model](#using-gemini-as-an-alternative-model)
- [Notifications](#notifications)
- [Known people](#known-people)
- [Where Blink keeps a clip, and the Sync now button](#where-blink-keeps-a-clip-and-the-sync-now-button)
- [Renaming a camera](#renaming-a-camera)
- [Retention](#retention)
- [Docker in depth](#docker-in-depth)
- [Reaching it from other devices](#reaching-it-from-other-devices)
- [Notes](#notes)

## Using it

- **Header** — `last checked` shows how long ago the most recent motion check
  completed, with the wall-clock time beside it. It turns amber if a check is
  more than two intervals overdue, and only advances on a clean pass, so a
  wedged poll loop or an unreachable Blink is visible rather than silent.
- **Grid** — every camera's most recent still, refreshed each poll, with the
  last thing it saw written underneath. `Disarmed` and `Offline` are flagged on
  the picture. Click any card to isolate it.
- **Camera view** — the still or clip, the description, and that camera's own
  recent motion. The arrows beside it (or `←` `→`) step through the cameras,
  and the library icon opens the archive already filtered to this one.
- **On motion** — the camera is isolated automatically, its still and clip are
  shown, and the description appears below once the model responds. The card
  in the grid also flashes.
- **Follow motion** (header switch, on by default) — turn it off to stay where
  you are. Motion then raises a popup in the corner instead: the camera, its
  picture, the time and — once the model answers — what it saw. Click one to
  open that clip. There is one popup per camera, so a burst updates it ("3
  clips") rather than stacking; up to four are shown, each lasts 30 seconds
  (paused while the pointer is over it), and opening a camera clears its
  popup. Motion never pulls you out of the library either, so there it always
  pops up, whatever the switch says.
- **Back** — the *All cameras* button, `Esc`, or the browser's own Back button.

The address follows the view — `#/camera/<slug>`, `#/library` with its
filters in the query — so Back and Forward move between views, a reload lands
where it was, and a camera or a filtered search can be bookmarked. On a phone,
Back closes the full-screen viewer rather than leaving the page.

While the tab is in the background, the number of motion events since it was
last looked at is shown in its title.

### Settings

The sliders icon in the header holds choices that belong to the browser
rather than the server, so a wall display and a phone can differ. They are
kept in that browser's local storage along with *Follow motion* and *People
only*.

- **Theme** — follow the system, or force light or dark.
- **Chime on motion** — a short, soft bell whenever a camera reports new
  motion, whether or not the page follows it. Not when a description arrives
  later, and several cameras at once make one chime. Browsers only let a page
  make sound after it has been clicked or typed in, so after a reload it stays
  quiet until the first touch — worth knowing on a wall display.
- **Desktop alerts** — a system notification for motion while the tab is in the
  background; the description replaces the "Describing…" one in place rather
  than notifying twice. This is separate from ntfy and fires for every event.
- **Keep screen awake** — stops the screen sleeping while the page is visible,
  for a tablet on the wall.
- **Push notifications** — pause the ntfy notifications for 1 hour up to 3
  days, or until turned back on; see [Notifications](#notifications). Only
  shown when `NTFY_URL` is set, and unlike the rest of this menu it is not
  per browser.
- **Known people…** — see [Known people](#known-people).
- **Full screen**, and **Keyboard shortcuts**.
- **Report a bug…** — opens a new GitHub issue with your Blynky, blinkpy and
  Python versions, camera counts and browser already filled in. It never
  includes camera names, addresses or account details, and nothing is sent
  until you submit the issue on GitHub.

The bottom of the menu shows which Blynky is running: the release tag and the
short commit, such as `v0.1.1 · 72ef0a5`. A build made after a tag shows
`git describe`'s form instead, `v0.1.1-3-gabc1234`.

Desktop alerts and keeping the screen awake need a secure context: they are
offered on `http://127.0.0.1` or over HTTPS (`tailscale serve`, a reverse
proxy), and not at all over plain HTTP on the LAN, where browsers refuse them.

The page can also be added to a phone's home screen, where it opens without
the browser's address bar.

### Keyboard shortcuts

| Key | Does |
| --- | --- |
| `G` | All cameras |
| `L` | Library |
| `/` | Search the library |
| `1`–`9` | Open that camera |
| `←` `→` | Previous / next camera; in the viewer, previous / next capture |
| `P` | Play the clip, or back to the still |
| `R` | Take a new picture |
| `S` | Sync now |
| `F` | Follow motion on or off |
| `Esc` | Back |
| `?` | The list of these |

## How old the stills are

Blink does not keep a live picture of each camera. It stores one thumbnail and
only replaces it when something explicitly asks the camera for a new photo, so
a camera left alone can sit on the same image for months. Polling more often
does not help — there is nothing new to fetch.

So each card says how old its picture actually is, in amber once it is over an
hour old, and the grid uses whichever image is newer:

- a frame lifted out of the most recent motion clip, which costs nothing
  because the clip was downloaded anyway, or
- Blink's stored thumbnail.

**Refresh** on a card (**New picture** in the isolated view) wakes that one
camera and takes a real photo, which takes a few seconds. Nothing does this on
a timer: waking a battery-powered camera costs battery, so it only ever happens
when you ask.

Nothing is ever prompted on the terminal, so this works the same whether you
run it by hand or under launchd/systemd.

## The library

Everything captured is written under `DATA_DIR` and stays there unless
[retention](#retention) is turned on, so plan for the disk to grow.

```
data/
  events/<camera-slug>/<timestamp>.mp4      motion clip
                       <timestamp>.jpg      Blink's stored thumbnail
                       <timestamp>-frame.jpg  frame sampled from the clip
                       <timestamp>.txt      the description
                       <timestamp>.json     sidecar describing the capture
  stills/<camera-slug>/<timestamp>.jpg      every still the Refresh button took
  latest/<camera-slug>.jpg                  the picture the grid is showing now
```

Each capture's still also gets a `<timestamp>-thumb.jpg` beside it: a 480×270
crop, the shape the library tiles and timeline rows draw it, at about a fifth
of the size. They are what make a page of 60 tiles a couple of megabytes
rather than eight. New captures get one as they are saved; an archive from
before thumbnails existed is filled in once, in the background, a few seconds
per hundred captures, and the full still is shown until then. A thumbnail is
remade if its still is newer, can be deleted freely, and is removed with its
capture by retention and moved with it by `scripts/rename_camera.py`.

The **Library** tab in the header opens the archive: newest first, grouped
by day, filtered by camera, by clips-versus-stills or by what was seen, and
searchable across the descriptions and by [tag](#tags). **Up to** jumps back to a given day,
showing that day and everything before it, without scrolling through
everything since. Clicking a capture opens it full-size — clips play in place,
and the arrow keys (or a swipe, on a touch screen) walk the archive without
leaving the viewer.

The JSON sidecars are what make the archive survive a restart. Each one records
the capture's real timestamp, camera and description alongside its media, so
the whole index can be rebuilt by walking the directory; the app does exactly
that at startup. Media saved before sidecars existed is still picked up, with
the timestamp read back out of the filename and the description out of the
`.txt` — the only thing lost is the timezone offset's sign, and Blink reports
UTC.

`latest/` only ever holds the current picture per camera, which is why
`stills/` exists: without it, each new picture from the Refresh button would
overwrite the last one. Identical stills are not archived twice.

## The timeline

Under the camera grid is a running list of the last `TIMELINE` motion events —
time, camera, and the description the model wrote — and each camera's detail
view carries the same list for that camera alone. Clicking any row opens that
event, and the row for the event already on screen is marked.

Words in a description that mean a person — person, man, woman, child,
someone, and the rest of the list in `PEOPLE` in `app.js` — are highlighted,
so a list of events can be scanned for the ones worth opening. Most of what
these cameras notice is animals, vehicles and weather. A negated mention —
"no people, animals or vehicles visible", the model's way of saying the clip
was empty — is left plain.

Either list can be narrowed to events with a person in them, and the library
has the same filter across every subject. Filtering swaps the source: the
unfiltered list under the grid is the in-memory feed the page already has,
while a filtered one is fetched from the archive, because the last 25 events
might hold only three with a person in them. An event still being described is
never hidden — it would vanish and reappear a minute later.

Several clips from one camera in quick succession — someone walking past sets
a camera off more than once — are folded into one row marked "3 clips", which
opens out to show each. A clip joins the burst when it starts within two
minutes of the previous one from that camera; on this archive clips from one
camera never start within 15 seconds of each other and the gaps pile up
through the first two minutes, so that is where one visit ends. The row shows
the first clip with a person in it, or failing that the first described one,
and is dated by the newest. The library is never folded: it is the archive,
and the viewer steps through every clip.

The two lists come from different places on purpose. The one under the grid
reads the same in-memory feed the page is handed on load, so it costs nothing
and updates as events arrive. A single camera's list cannot: that feed holds
the last events across *every* camera, which for a quiet camera is often none
of them, so the detail view asks the archive for that camera's own last
`TIMELINE` instead. Live events are merged into whichever lists are already
loaded, and a reconnect throws the per-camera lists away — anything captured
while the socket was down was broadcast to nobody.

Cameras do not agree on shape: a doorbell records 1920x1920,
the rest 1280x720 or 640x360. Both players are therefore sized by their box
rather than by the clip, and letterbox whatever does not match, so the layout
does not jump between cameras and a square clip cannot outgrow the window. The
shape is the `--player` custom property in `style.css`.

A disarmed sync module reports no motion at all, even though clips still
record. Cards show a `disarmed` badge so this is visible rather than puzzling.

`MAX_TOKENS` is generous because reasoning models spend tokens thinking before
answering; too small a budget yields an empty response rather than an error.

`.blink_creds.json` contains your Blink password in plaintext (blinkpy stores
it alongside the tokens). It is chmod 600 and gitignored.

## What the model saw

Each description comes back with a short, closed list of what the model
actually saw — `person`, `animal`, `vehicle`, `package` — stored alongside the
sentence in the sidecar. This is what the filters and the notifications key
off, because free prose cannot be filtered reliably: the word "person" appears
in "no people visible" and in "a mannequin" exactly as it does in a burglar.

A model that answers in prose instead of JSON still works. The sentence is
kept as the description and the subjects are left unset, which means "not
known" rather than "nothing was there". Descriptions written before this
existed have their subjects inferred from the wording and marked
`subjects_from: "text"`, so a guess is never passed off as an observation.

### Tags

Every capture shows its tags under the description: `#person` and the
other subjects, then any of a few finer tags the model may add — `#dog`,
`#cat`, `#deer`, `#bird`, `#bicycle` and `#delivery` (a courier dropping
something off or collecting it) — then the name of anyone
[recognised](#known-people), such as `#alice`. The model is told most clips
need no finer tags and may give at most three, so a tag stays worth searching
for. Like the subjects, the list is closed: a word outside it is dropped, so a
search never has to guess what the model might have called something.

Type a tag into the library search, or click one on a capture, to filter by
it. Tags and words combine: `#dog #delivery front door` is everything tagged
both `#dog` and `#delivery` whose description or camera mentions "front
door". The search box suggests the tags in use, most common first.

Subject and name tags come from what is already stored, so older captures
have them too. The finer tags are only asked for in new descriptions, so
captures described before they existed have none. A recognised name is a
guess unless someone confirmed it, and its tag is shown dashed until then.

Clips that never got a description — the model timed out, or answered with
nothing — are described at start-up, oldest first, and the library offers a
button for the same thing. Each attempt is counted and a clip is dropped after
`DESCRIBE_ATTEMPTS`, so one the model can never handle is not retried forever.
A description arriving for a clip captured weeks ago updates the row it
belongs to and nothing else: it does not flash a card, jump the view, or take
the top of the timeline.

## Using Gemini as an alternative model

By default, Blynky uses a local vision model hosted on Ollama. If you prefer not
to install, configure, or run Ollama on your machine—or want to run Blynky on
low-power hardware without a dedicated GPU—Google Gemini can be used as an
alternative cloud vision provider.

### Recommended model: `gemini-3.5-flash-lite`

`gemini-3.5-flash-lite` is the recommended model (and the default when Gemini
is selected). It produces fast, high-quality descriptions and accurate subject
tagging, while remaining within Google's free-tier limits (such as 15 requests
per minute and 1,500 requests per day), which is more than enough to cover
typical home camera activity.

### Getting an API key

1. Visit [Google AI Studio](https://aistudio.google.com/).
2. Sign in with a Google account.
3. Click **Get API key** and create a key in a new or existing project.

### What to change in `.env`

In your `.env` file, set `VISION_PROVIDER` to `gemini` and add your API key:

```bash
# Switch from ollama to gemini
VISION_PROVIDER=gemini

# Your Gemini API key from Google AI Studio (required when VISION_PROVIDER=gemini)
GEMINI_API_KEY=your-gemini-api-key-here
```

Optional settings for Gemini:

- `GEMINI_MODEL` — defaults to `gemini-3.5-flash-lite`. Can be pointed to other
  models such as `gemini-3.5-flash` if desired.
- `GEMINI_PAUSE` — minimum seconds to wait between vision requests (e.g. `4.5`
  to avoid exceeding the free tier's 15 RPM limit during bursts or backfill).
  Defaults to unset (`PROVIDER_PAUSE`, which is `0`).
- `GEMINI_URL` — defaults to `https://generativelanguage.googleapis.com/v1beta/openai`
  (Gemini's OpenAI-compatible endpoint).

## Notifications

Set `NTFY_URL` to an ntfy topic and a camera seeing a person will push the
description and the frame to your phone. Nothing is sent while the variable is
empty.

Only `NTFY_SUBJECTS` are worth sending — people by default. Two thirds of what
these cameras record involves a person and nearly all the rest is animals,
vehicles and weather, so alerting on everything would be alerting on nothing.
An event whose subjects are unknown is never sent: waking a phone on a guess
is how notifications end up muted. `NTFY_COOLDOWN` bounds one camera stuck
facing a swaying branch, and the backlog sweep never notifies at all.

**Push notifications** in the settings menu pauses them — for a party, a
day of contractors, or a camera you are working on. Pick how long under
*Pause for* and turn the switch off; they come back on by themselves when the
time is up, or when the switch is turned back on. Changing the length while
paused restarts the pause from now. The server is what sends them, so a pause
holds for everyone, every open page shows it, and a header pill says *Push
paused* until it ends. It is kept in `DATA_DIR/ntfy.json`, so a restart
neither forgets it nor ends it early. The same switch is `POST /api/ntfy`
with `{"paused": true, "hours": 4}`, `{"paused": true}` or `{"paused": false}`.

The frame travels as the request body, because a loopback-bound archive is not
somewhere ntfy could fetch an attachment from. Note that an ntfy.sh topic is
readable by anyone who knows its name, and these messages say who is at the
house and when.

## Known people

Open a capture in the library, and if it has a person in it the viewer asks
"Who is this?". Naming them keeps that frame as a reference, and from then on
the model is shown those references before it looks at a new clip.

**This is not face recognition, and it could not be.** These cameras are
wide-angle and mounted high: a person is a hundred-odd pixels tall and a face
about twenty, which is nothing for an embedding to work with. What the model
does instead is judge build, posture, hair and context against labeled
examples. Measured on this archive that survives a change of clothes — the
same man was picked out in a dark tee and again in an orange shirt from
references that were all one afternoon in maroon, with the model explicitly
saying the shirt was not its reason. It also matters that identification runs
on the clip's frames rather than the saved still: the still is the clip's last
frame and often no longer has the person in it, and one probe that answered
"no person is visible" from the still identified them with high confidence
from four.

It is always a guess, and the UI says so — "Looks like Alex", never "Alex".
`IDENTIFY_MIN_CONFIDENCE` defaults to `high` because the two mistakes do not
cost the same: failing to recognise someone just means the ordinary alert
arrives, while a wrong match quietly downgrades the alert about a stranger,
which is the one worth having. Testing produced a medium-confidence match on
evidence as thin as "lean build and dark hair", which is what that floor is
there to reject.

Tagging is also how a wrong guess gets fixed. Naming the person it got wrong
adds that frame as an example, so the next attempt has more to go on. Spread
references over several days rather than piling them up from one event, and
enrol two or three people and watch it for a week before trusting it to
silence anything.

A recognised person notifies at `NTFY_KNOWN_PRIORITY` instead of
`NTFY_PRIORITY` — quiet for the household, loud for anyone else — and
`NTFY_SKIP_KNOWN=1` silences them entirely. Anyone unrecognised keeps the
louder priority, which includes every event where identification never ran.

**Known people…**, in the settings menu, shows every enrolled person with
the frames they are recognised by. A frame that shows the wrong person, or
somebody else alongside them, teaches the model the wrong thing, so check it
now and then and remove any that do not belong; *Forget* removes the person
and all their frames. Both ask twice. Removing a frame does not change what
earlier captures were marked as.

References live in `DATA_DIR/people/<slug>/`, a manifest beside the frames,
the same self-describing shape as the rest of the archive.

## Where Blink keeps a clip, and the Sync now button

A poll only ever sees what Blink is willing to report as *new motion since the
last check*, and there are several ways for a clip to exist without ever being
reported that way:

- **The app was not running.** Nothing is caught up afterwards; a poll asks
  what has moved, not what it missed.
- **A burst.** One person walking past sets off several clips seconds apart.
  blinkpy returns all of them, but the camera object only remembers the last,
  and reading only that threw away roughly half of everything recorded. Every
  record in a refresh is now saved, oldest first.
- **No subscription.** Clips reach Blink's cloud feed only while a plan is
  active. Without one they are written to the sync module's own USB storage
  instead, which is a separate listing that has to be asked for each time, and
  the sync module declines to answer while it is busy saving a clip.
- **The sync module does not offer local storage.** A module whose record
  says `local_storage_compatible: false` is not used by every camera on its
  network: battery cameras still route clips onto its card, but Minis write
  nothing to it, ever. With no subscription those clips exist only in the
  Blink app and reach no API this can read, so nothing here can recover them.
  Minis are perfectly capable of writing to a card - on a Sync Module 2 they
  do - so a Mini with no clips on the card points at the module, not at the
  camera.

**Sync now**, in the header, is the answer to the first three. It asks Blink
directly for the most recent `SYNC_LIMIT` clips on every camera — reading both
the sync modules' storage and the cloud feed — and saves whatever is not in
the archive already, ignoring the poll cursor entirely. Anything it saves is
described, indexed and shown like any other capture. It skips what is already
saved, so pressing it twice costs one listing and no downloads, and the button
says what the run found rather than silently going quiet.

## Renaming a camera

Blink knows a camera by a number, but blinkpy — and so everything keyed off it
here — knows it by name, and reads the names once, at start-up. Renaming a
camera in the Blink app while this is running used to leave blinkpy's tables
answering to the old name: an `Error getting last records for '<new name>'`
traceback on every poll, and none of that camera's clips found. A rename is
now noticed on the next poll and blinkpy's tables are rebuilt from the
homescreen it has just fetched, without logging in again.

The harder part is the sync module's card. Each clip on it is labelled with the
name the camera had **when it recorded**, and that label never changes, so after
a rename every clip already on the card belongs to no camera at all. To keep
them reachable, `data/cameras.json` records every name each camera id has gone
by, and clips under a former name are handed to the camera that now has it.
Captures already in the archive stay filed under the name they were saved
with, but they still count: nothing under either name is downloaded twice, and
the renamed camera's card and poll cursor pick up where the old name left off.

Renames that happen while the app is running, or between one run and the next,
are recorded without any help. A rename made before `cameras.json` existed has
nothing to be recognised by, so name it once in `.env`:

```
CAMERA_RENAMES=Old Front Door=Front Door
```

It is safe to leave in place afterwards.

### Adding, removing or replacing a camera

Deleting a camera in the Blink app and adding it back gives it a **new id**
under the same name, and blinkpy reads ids once, at start-up. A wired camera
is then looked up by an id that no longer exists, so it stops updating until
the app restarts. Cameras added, removed or added back are now noticed on the
next poll alongside renames, and blinkpy's tables are rebuilt the same way.
The names the camera went by are carried onto its new id, so clips still on a
sync module's card under an older name keep matching it, and a camera that is
gone for good loses its card in the grid instead of sitting there stale.

### Moving cameras between systems

A camera moves to another Blink system - say, from a Sync Module XR to a Sync
Module 2 - by being deleted and added to the other one, so it is the same
case as above: a new id under the same name, picked up on the next poll, with
its history kept. Keep the name and its archive carries straight on.

Systems themselves being added, renamed or removed are noticed too. blinkpy
files each sync module under its system's name and, left to itself, never
forgets one: a renamed system would leave its old module behind, still polled
every cycle and still holding cameras that have since moved and been given
new ids. So when the list of systems changes, blinkpy's tables are rebuilt
from scratch rather than added to; if Blink fails part-way through, the old
tables are kept and the rebuild is retried on the next poll. Sync now reads
the card or drive of every sync module in the current line-up.

The camera view shows which system each camera is in, since arming and
disarming are per system: a camera moved into a system that is disarmed
records nothing, however healthy it looks.

One warning is worth expecting and ignoring:

```
Could not find thumbnail for camera 'Upstairs Hallway'
```

Blink has no picture for a freshly added camera until it takes one, which can
be hours. It comes from blinkpy, repeats every poll until the first thumbnail
exists, and needs nothing done about it. The camera's card stays blank for
that long; motion is unaffected.

### Refiling what was already captured

The archive itself is left under the old name: its folders, the name inside
each sidecar, and the events a known person's reference frames came from. The
monitor copes with that, but the library keeps listing the old name as a
separate camera. `scripts/rename_camera.py` refiles all of it under the new
name, and adds the old name to `cameras.json` if the camera is already there.

Stop the monitor first — a running one holds the old paths and would serve
missing files — and start it again afterwards. Nothing changes without
`--apply`, so run it once without to read what it will do:

```sh
# uv / a plain python3 on the host (no dependencies needed)
python3 scripts/rename_camera.py "Old Front Door" "Front Door" --data ./data
python3 scripts/rename_camera.py "Old Front Door" "Front Door" --data ./data --apply
```

Under Docker the files belong to the container's user, so run it in the image
(rebuild first, so the image has the script):

```sh
docker compose build
docker compose stop blynky
docker compose run --rm --no-deps blynky \
  python scripts/rename_camera.py "Old Front Door" "Front Door" --apply
docker compose up -d
```

It never overwrites. A capture whose files clash with ones already under the
new name stays in the old folder and is listed, and the exit status is 1. An
interrupted run — a permissions error, say — leaves the archive consistent,
and running it again carries on from there.

## Retention

By default nothing is ever deleted; expect an active set of cameras to add
roughly a gigabyte every two weeks.
`RETAIN_DAYS` deletes captures older than that many days — age only, whatever
is in them, so what is on disk follows from the calendar rather than from what
a model once said. It defaults to `0`, meaning keep everything: upgrading must
not start deleting footage on its own.

Age is read from the capture's own timestamp, never the file's mtime — this
archive was backfilled from filenames long after the events happened, so mtime
would delete almost exactly the wrong things. `latest/` is never touched; it
holds the picture each card is showing, which is not archive. Set
`RETAIN_DRY_RUN=1` for a run that only logs what it would remove.

## Docker in depth

The image carries `ffmpeg`, runs as an unprivileged user, and has a
healthcheck on `/api/state`, so `docker compose ps` distinguishes "listening"
from "wedged".

### Where blinkpy comes from

Running with `uv` uses the `../blinkpy` checkout. The image does not: it
fetches blinkpy from GitHub at a commit pinned in the `Dockerfile` and
installs that. So the container builds on a
machine that has only this repo, and the build is reproducible rather than
dependent on whatever state a sibling directory happens to be in.

It comes from [gudlyf/blinkpy](https://github.com/gudlyf/blinkpy), a fork of
[fronzbot/blinkpy](https://github.com/fronzbot/blinkpy), so the build does not
break if upstream goes away. The fork has two branches that matter:

- **`dev`** is an exact copy of upstream's `dev`, kept current with GitHub's
  *Sync fork* button. Nothing else is ever committed to it.
- **`blynky`** is `dev` plus Blynky's own fixes, one commit each, with the
  commit message saying what the fix is and why. The pin is always a commit
  on this branch.

The pin is a commit, not a version, because upstream `dev` has fixes that are
not yet in any released blinkpy version. The fork does not make the build
trust anything new: git checks the fetched code against the pinned SHA, so a
commit cannot be swapped out in either repo.

The fixes on `blynky` today:

- **Harden `validate_homescreen`.** Upstream assumes the homescreen request
  succeeded and returned JSON. When Blink answers with an HTML error or
  throttling page, the log says only `ContentTypeError`, with no status or
  body. The fix logs the status and the start of the body and raises
  `BlinkSetupError` instead. Diagnostics only: Blynky already recovers from
  the failure, it just doesn't say why without this.

To move to a newer upstream:

```bash
# in a clone of gudlyf/blinkpy, with fronzbot/blinkpy as the upstream remote
git fetch upstream
git push origin upstream/dev:dev          # or Sync fork on GitHub
git checkout blynky
git rebase upstream/dev                   # conflicts mean a fix needs redoing
git log --oneline upstream/dev..blynky    # check it's just Blynky's fixes
git push --force-with-lease origin blynky
git tag -a "blynky-$(date +%F)" -m "Pinned by Blynky"
git push origin "blynky-$(date +%F)"      # keeps the commit after rebases
git rev-parse HEAD                        # the new BLINKPY_REF
```

Drop a fix from `blynky` once upstream has its own. Then set `BLINKPY_REF` in
the `Dockerfile` to the new commit and rebuild, and check out the same commit
in `../blinkpy` so the container and `uv run` stay on the same code.

Because `blynky` is rebased, older pinned commits drop off the branch. The
`blynky-<date>` tag is what keeps each one reachable, so older Blynky
checkouts still build: don't delete those tags.

### Persistence

`./data` is bind-mounted to `/data`, and `CREDFILE` points inside it, so the
token file survives `docker compose down` and a restart does not send you back
to the sign-in panel. Clips and stills land there too, and stay readable from
the host — that is why it is a bind mount rather than a named volume.

If you have already signed in outside Docker, move the existing token in
rather than authenticating a second time:

```bash
cp .blink_creds.json data/
```

### Configuration under compose

`.env` is read as usual, but four variables are pinned in `docker-compose.yml`
and win over it, because in a container the file's values are wrong:

| Pinned | Why |
| --- | --- |
| `HOST=0.0.0.0` | A container that binds loopback is unreachable from the host. The container's network namespace is the isolation boundary here, not the bind address. |
| `PORT=8080` | Fixed so it cannot drift out of sync with the port mapping. |
| `DATA_DIR` / `CREDFILE` | Point at the mounted `/data` instead of the image's ephemeral filesystem. |

Publishing is controlled separately, and defaults to loopback for the reason
described in [Reaching it from other devices](#reaching-it-from-other-devices)
— the UI still has no authentication of its own. In `.env`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `BIND_ADDR` | `127.0.0.1` | Host interface the port is published on |
| `HOST_PORT` | `8080` | Host port |

So `tailscale serve --bg --https=443 8080` works unchanged against the
default, and exposing the app more widely means setting `BIND_ADDR` — a
deliberate edit, not a side effect of containerising.

### Reaching Ollama from the container

Two things have to be true, and only one of them is about DNS.

**The name has to resolve.** Docker Desktop and OrbStack both define
`host.docker.internal`; Docker Engine on Linux does not. `docker-compose.yml`
pins it to `host-gateway`, which is a no-op where the name already works and
makes the file portable to Linux.

**Ollama has to listen on more than loopback.** This is the one that actually
bites. Ollama binds `127.0.0.1` by default, and no container can reach that no
matter how the name resolves. The failure is a refusal, not a DNS error, which
is why it reads as a connection problem against a perfectly correct address:

```
vision request failed: Cannot connect to host host.docker.internal:11434
  ssl:default [Connect call failed ('0.250.250.254', 11434)]
```

That address is the host, and it is right. Nothing was listening on it.

On macOS, turn on the "Expose Ollama on the network" setting in Ollama.app, or
set `OLLAMA_HOST=0.0.0.0:11434` and restart it. On a systemd host:

```sh
sudo systemctl edit ollama    # [Service] / Environment="OLLAMA_HOST=0.0.0.0:11434"
sudo systemctl restart ollama
```

Check it from the host first — `curl -s localhost:11434/api/tags` should list
models, and the listening socket should be `*:11434` rather than
`127.0.0.1:11434` — then from inside the container:

```sh
docker compose exec blynky \
  python -c "import urllib.request;print(urllib.request.urlopen('http://host.docker.internal:11434/api/tags',timeout=5).status)"
```

`200` means descriptions will start working. Pointing `OLLAMA_URL` at a LAN or
tailnet address instead is equally fine, and still needs Ollama listening on
the interface that address belongs to.
## Reaching it from other devices

The UI has **no authentication of its own**. Anyone who can reach the port
sees your camera stills, clips, and descriptions — and if the sign-in panel
happens to be up, can submit credentials over plain HTTP. The default
`127.0.0.1` binding keeps that on this machine.

Over Tailscale, best to worst:

1. **`tailscale serve`** — leave the app on `127.0.0.1` and front it:

   ```bash
   tailscale serve --bg --https=443 8080
   ```

   Real HTTPS on your MagicDNS name, tailnet-only, nothing bound to a routable
   interface. The WebSocket upgrade is proxied, and the page switches itself to
   `wss://` when served over HTTPS.

2. **Bind the tailnet IP** — `HOST=100.x.y.z uv run run.py`. Reachable from
   tailnet devices only; LAN and public interfaces stay closed. WireGuard
   encrypts transit, but the browser sees plain HTTP.

3. **`HOST=0.0.0.0`** — binds every interface, including whatever Wi-Fi you
   are on. Avoid unless you know the network.

Never put this behind `tailscale funnel`: that publishes it to the open
internet, and there is no login to stop anyone who finds it.

## Notes

Blink has no push API, so this polls. blinkpy's README warns that calls faster
than 60s can overwhelm Blink's servers, so `POLL_SECONDS` is floored at 60 and
`refresh()` is called without `force=` to leave the library's own throttling
intact. Expect tens of seconds between real-world motion and the event
appearing — the clip has to finish uploading to Blink first.

The live event feed a page gets on load is capped at `HISTORY` and is motion
only — stills are already visible on the cards. Neither cap touches what is on
disk; the library reads from the archive itself.
