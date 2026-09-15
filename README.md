# easyfile

Easier than `pathlib`. Safer than `os`/`shutil`. Zero dependencies.

```python
from easyfile import File, Dir

File("notes/todo.txt").write("buy milk")     # auto-creates notes/
File("notes/todo.txt").append("\nwalk dog")
File("notes/todo.txt").read()                # "buy milk\nwalk dog"

File("config.json").write_json({"debug": True})
File("config.json").read_json()              # {"debug": True}

Dir("build").clear()                          # safe — moves to .trash/, not gone forever
File("secret.key").delete()                   # safe delete, recoverable
File("secret.key").delete(permanent=True)     # actually gone
```

## Why not just pathlib?

| | pathlib | easyfile |
|---|---|---|
| Write text | `p.parent.mkdir(parents=True, exist_ok=True); p.write_text(s)` | `File(p).write(s)` |
| Crash-safe writes | manual temp-file dance | **atomic by default** (temp file + `os.replace`) |
| Accidental delete | gone forever | **moved to `.trash/`** unless `permanent=True` |
| Untrusted path join | vulnerable to `..` traversal | `safe_join()` / `Dir.child()` raise `PathEscapeError` |
| Zip extraction | vulnerable to zip-slip | `Dir.unzip()` validates every entry |
| JSON / CSV / hashing | import 3 more modules | built in |
| Overwrite protection | none by default | `overwrite=False` raises `FileExistsError` |

## Install

Just drop `easyfile.py` in your project — no dependencies, pure stdlib.

## API overview

### `File`
`read` / `read_bytes` / `read_lines` / `iter_lines` / `read_json` / `read_csv`
`write` / `write_bytes` / `write_json` / `write_csv` / `append` / `touch`
`copy_to` / `move_to` / `rename` / `delete(permanent=False)` / `backup` / `replace_text`
`hash()` / `size()` / `modified()` / `is_empty` / `exists`

### `Dir`
`make` / `list` / `files` / `dirs` / `find` / `child` (safe join)
`copy_to` / `move_to` / `clear(permanent=False)` / `delete(permanent=False)`
`zip_to` / `Dir.unzip` (zip-slip safe) / `size()` / `is_empty`
`snapshot()` / `diff_since()` — cheap way to detect what changed in a folder

### Module-level
`safe_join(root, *parts)` — join untrusted path segments, raising `PathEscapeError`
instead of allowing `..`-style escapes outside `root`.

## Safety guarantees

1. **Atomic writes** — every `write*` call writes to a temp file in the same
   directory, `fsync`s it, then does an atomic `os.replace`. A crash or power
   loss mid-write can never leave you with a truncated/corrupt file.
2. **Safe deletes** — `delete()` and `clear()` default to moving things into
   a local `.trash/` folder (timestamped) instead of permanently destroying
   data. Pass `permanent=True` when you really mean it.
3. **No silent overwrites** — most operations accept `overwrite=False` to
   raise `FileExistsError` instead of clobbering existing data.
4. **Path traversal protection** — `safe_join`, `Dir.child`, and
   `Dir.unzip` all validate that the resulting path can't escape the
   intended root, closing off a whole class of directory-traversal and
   zip-slip vulnerabilities.
5. **Clear exceptions** — `NotAFileError`, `NotADirError`, `PathEscapeError`
   instead of decoding raw `OSError` codes.

## License

MIT — do whatever you want with it.
