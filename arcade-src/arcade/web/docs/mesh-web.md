# The .mesh web, and a site on any name

A small web for the LoRa mesh, made the way the web was in 1990: plain text, headings, lists and links, nothing else.
Every page belongs to a `.mesh` name, and the wallet holding the name decides what its page says. A DogecoinArcade
T-Deck reads pages in the **Browser** app (from its App Store), over WiFi or by radio.

The same **Browser** on the PWA and the website (`/browser`) opens the site of **any** name: `.mesh`, `.pepe`, `.doge`
and every other ending, on every chain the arcade reads. Those sites are not limited the way `.mesh` ones are; see
[Sites on any name](#sites-on-any-name).

## A name

A `.mesh` name is an ordinary name inscription on **Pepecoin mainnet**:

```
{"p":"dns","op":"reg","name":"yours.mesh"}
```

Claim one from **Creator → Names**. The first claim of a name owns it, and whoever holds that inscription holds the
name: send it to someone and the name goes with it.

## A page

A page is another inscription on Pepecoin mainnet, made to the address that holds the name:

```
{"p":"mesh","name":"yours.mesh","pages":{"/":"# Welcome\n...","/about":"..."}}
```

- `pages` holds the whole site: `/` is the front page; other paths are short words (`/about`, `/notes/today`) of
  lowercase letters, digits, `.`, `_` and `-`, up to 24 of them.
- The whole inscription is at most **3,000 bytes**, so every page loads over LoRa too.
- **Which page counts:** the newest page inscribed to the address that holds the name *now*. Publish again to change
  your site. If the name is sold, your pages stop counting, and the new holder's pages count instead.

**Creator → Names → Publish a page** writes this for you, with a preview at the T-Deck's size.

## MeshText

Each page is plain text, one line at a time:

| Line | Shows as |
|---|---|
| `# Title` | a big heading (the first one is the page's title) |
| `## Section`, `### Small` | smaller headings |
| `* item` | a list item |
| `> words` | a quote |
| `=> other.mesh/path Label` | a link to another site; `=> /path Label` is a page of this site |
| `![What it shows](inscription id)` | a picture: any image inscription |
| ```` ``` ```` | starts or ends preformatted text, kept exactly (ASCII art) |
| anything else | a paragraph line |

There are no colours, fonts or scripts. The T-Deck draws every page in its own font on a 320 × 240 screen.
Short lines read best.

## Pictures

A picture is its own inscription, so it does not count toward the 3,000 bytes. Put any image inscription on a page:

```
![Sunset over the lake](d737d773afcafa0b445def80f4dc43fc596cf185eac93941702675df384ac260i0)
```

A browser shows the inscription itself. A T-Deck gets it the way the web drew pictures in 1994, **from the top down**:
the arcade makes it at most 300 × 200 in 16 colours, in bands of rows that each fill about one LoRa part, so every part
that arrives draws a few more rows. At most about 8 KB, about a minute by radio and instant over WiFi. A T-Deck keeps
the last 16 pictures it read and never asks for those again. Small, flat pictures (pixel art, 16 colours or fewer)
come through exactly and in a few hundred bytes.

```
# Me-Seeks
Hello from the T-Deck web!

## Around here
=> /about About me
=> arcade.mesh The arcade's own site

* Ask me anything over LoRa.
```

## Reading pages

- **On a T-Deck:** the Browser asks a gateway, over WiFi or LoRa. A page it has already read comes from its own
  memory unless the site changed. Any site can be set as its homepage.
- **In a browser:** `https://app.dogecoinarcade.com/meshweb/yours.mesh` shows a page at the T-Deck's size.
  `/meshweb` lists every site.
- **For programs:** `GET /r/mesh` is the directory (every name with a page, newest first);
  `GET /r/mesh/yours.mesh/about` is one page as JSON: `body` (its MeshText), `title`, `paths`, `page` (the
  inscription), `holder`, `pictures`. Add `?have=<page>` and an unchanged site answers `{"unchanged": true}` without
  its body. `GET /r/mesh/picture/<inscription id>` is a picture as a T-Deck draws it (the format is in
  `arcade/mesh_picture.py`).

## Sites on any name

Any p:dns name can have a site, made the same way: a `{"p":"mesh","name":"yours.pepe","pages":{...}}` inscription to
the address holding the name, on the chain the name lives on. The newest one by the holder counts. **Creator → Names →
Publish a site** writes it for you. Read in the Browser on the PWA and the website, so not limited for LoRa:

- no 3,000-byte cap: the record can be up to 64 KB, and its pages can be inscriptions of their own of any size;
- a page can be MeshText (pictures and all), **HTML** (`{"html": "<!doctype html>..."}`), or **another inscription**
  (`{"id": "<inscription id>"}`: a page, a picture, a game);
- HTML runs sandboxed, the way every inscription does: it reaches nothing of the arcade's and sends no cookies. Use
  relative links between its pages (`about`, not `/about`).

For programs: `GET /r/sites?page=0&per=50` lists every site on every chain, newest first, a page at a time
(`total`, `pages`), each with its `chain`; `GET /r/site/yours.pepe/about`
is one page (`kind`: `text` with `body`, or `html` / `id` with a `url` to show it in a frame); `?chain=` picks the
chain when a name has a site on more than one (it answers 300 with the `choices` otherwise). A page of a site on its
own is `/site/<chain>/yours.pepe/about`.

A site shown in the Browser can open another name in it: post `{arcade: "browser", go: "yours.pepe/about"}` to
`window.parent`. Only the frame the Browser drew is listened to.

## Searching every site

**Me-Seeks** (`me-seeks.pep`, the Browser's homepage unless you pick another) searches every site. It is an
inscription that asks `GET /r/search?q=frog&page=0`: 10 results a page (`results`: `name`, `path`, `address`,
`chain`, `title`, `snippet`; `total`, `pages`). The words that matched sit between `\x02` and `\x03` in the snippet;
draw it as text, never as markup.

The index lives on the arcades that choose to host it: **Admin → DNS → Sites search**, per chain, **Host**, **Ask a
linked arcade** (the default) or **Off**. Hosting needs that chain's full ord index ("Keep the full index"), and keeps
which page has which word (title, description, headings and up to 4,000 words of text) in `sites-<chain>.sqlite`,
with a short excerpt a page: about 1.5 KB a page, about 1.5 GB a million pages. Each host tells the network how big
its copy is, and the DNS tab shows it before you choose Host. Every hosting arcade builds it from the chain alone, by the same rule the Browser reads sites by, so any two give
the same answers. An arcade that does not host asks a linked one that does.

**Being found.** What counts most is your name, then your page's `<title>`, its `<meta name="description">` (shown as
the snippet), its headings and its words. A word counts at most eight times a page, so repeating it
does not help. A site's front page comes before its other pages. A newer site record replaces the old one in the
index when it is mined and read.
