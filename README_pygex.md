# PyGex

A PascalCase, decorator-driven wrapper around `pygame`. Faster to write,
harder to misuse, optimized in the places pygame code usually gets slow.

```python
from pygex import Game, Input, Colors, Vector2

App = Game(Width=800, Height=600, Title="My Game")
Player = Vector2(400, 300)

@App.Start
def Init():
    print("Game starting")

@App.Update
def Tick(Dt):
    Speed = 200 * Dt
    if Input.KeyDown("d"): Player.X += Speed
    if Input.KeyDown("a"): Player.X -= Speed
    if Input.KeyDown("s"): Player.Y += Speed
    if Input.KeyDown("w"): Player.Y -= Speed

@App.Draw
def Render(Surface):
    import pygame
    pygame.draw.circle(Surface, Colors.White, Player.IntTuple(), 20)

App.Run()
```

## Install

```
pip install pygame
```
then drop `pygex.py` in your project (single file, one dependency: pygame itself).

## Why

pygame's own API is `snake_case`, event-loop-driven, and leaves every
optimization up to you. PyGex is PascalCase throughout, replaces the
manual event loop with `@Game.Start` / `@Game.Update` / `@Game.Draw`
decorators, and bakes in the optimizations you'd otherwise have to
remember to do yourself:

| Concern | Raw pygame | PyGex |
|---|---|---|
| Main loop | hand-rolled `while running:` | `@App.Start/Update/Draw` + `App.Run()` |
| Image loading | `.load()` then remember `.convert_alpha()` | `Assets.Image()` — converted once, cached forever |
| Text rendering | re-rendered every frame (slow) | `Label` — only re-renders when text/color changes |
| Bullets/particles | allocate + garbage-collect every frame | `Pool` / `ParticleSystem` — pre-allocated & reused |
| Sprite updates | manual Python loops | `Group` — batches through pygame's C-level `Group.update/draw` |
| Input state | `pygame.key.get_pressed()` each check | `Input` — polled once/frame, cached, `KeyPressed`/`KeyReleased` edge detection included |
| Key codes | `pygame.K_a` constants to memorize | `Input.KeyDown("a")` — string keys, resolved & cached once |
| Distance checks | `math.hypot` (sqrt) even when unnecessary | `Vector2.DistanceSquared` / `CircleCollide` avoid sqrt |
| Errors | raw `pygame.error` | `AssetError`, `InitError`, `PyGexError` — clear messages |

## What's included

- **`Vector2`** — slotted 2D vector: `Length`, `Normalized`, `Lerp`, `Dot`, `Rotated`, `FromAngle`, `DistanceSquared`, etc.
- **`Rect`** — PascalCase facade over `pygame.Rect`.
- **`Colors`** — named color constants + `Colors.Random()`.
- **`Input`** — cached keyboard/mouse polling: `KeyDown`, `KeyPressed`, `KeyReleased`, `MouseDown`, `MouseClicked`, `MousePos`, `MouseWheel`.
- **`Timer`** — countdown/repeating timer with `OnComplete` callback and `.Progress`.
- **`Tween` / `Easing`** — animate a value over time with easing curves (`EaseInQuad`, `EaseOutBounce`, etc.).
- **`Assets`** — cached image/sound/font loading; images are `.convert_alpha()`'d once at load.
- **`SpriteSheet` / `Animation`** — slice a sheet once, play frames at a given FPS.
- **`Sprite` / `Group`** — PascalCase sprite base class + group, riding on pygame's fast batch update/draw.
- **`Pool`** — generic object pool for anything spawned/destroyed frequently.
- **`Particle` / `ParticleSystem`** — pooled particle emitter, no per-frame allocation.
- **`Camera`** — scrolling offset with optional smoothing and world bounds.
- **`Label` / `Button`** — minimal UI with cached text rendering.
- **`Scene` / `SceneManager`** — swap game screens (`Menu`, `Playing`, `GameOver`, ...) cleanly.
- **`CircleCollide` / `PointInCircle`** — sqrt-free collision helpers.
- **`Game`** — the engine core: `@Game.Start`, `@Game.Update`, `@Game.Draw`, `@Game.OnQuit`, `Game.Track(Timer)`, `Game.Run()`, `Game.Quit()`.

## Reliability notes

- Every class that's instantiated a lot (`Vector2`, `Rect`, `Timer`, `Particle`, `Sprite` internals) uses `__slots__` to cut memory overhead and speed up attribute access.
- Asset loading failures raise `AssetError` with the path and underlying pygame error — never a silent `None`.
- `Game.Screen` raises `InitError` if accessed before `Run()` has created the window, instead of a confusing `AttributeError` on `None`.
- `ParticleSystem.Emit` silently caps at the pool size instead of crashing or unbounded-allocating when you ask for more particles than you provisioned.
- `Pool.Release` is a no-op (not a crash) if you release an object that was never acquired or was already released — double-release is safe.

## Full multi-callback example

```python
from pygex import Game, Timer, Colors

App = Game(Width=640, Height=480, Title="Timers")

@App.Start
def Init():
    App.Track(Timer(2.0, OnComplete=lambda: print("2 seconds passed"), Repeating=True))

@App.Update
def Tick(Dt):
    pass  # timers registered via App.Track update themselves

@App.Draw
def Render(Surface):
    Surface.fill(Colors.DarkGray)

App.Run()
```

You can register multiple `@App.Update` / `@App.Draw` functions — e.g. one
per module (`player.py`, `enemies.py`, `ui.py`) — and they all run in
registration order every frame.

## License

MIT.
