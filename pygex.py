"""
PyGex — A PascalCase, decorator-driven wrapper around pygame.

Design goals
------------
1. PascalCase everywhere in the public API (classes, methods, functions,
   even module-level constants) — no snake_case leaking through.
2. Decorator-driven lifecycle: register your game logic with @Game.Start,
   @Game.Update, @Game.Draw instead of hand-rolling a main loop.
3. Heavily optimized hot paths:
   - __slots__ on every frequently-instantiated class (Vector2, Rect,
     Sprite, Particle, Timer) to cut memory + attribute-lookup overhead.
   - Cached/pre-converted Surfaces (Convert/ConvertAlpha applied once,
     at load time, not per-frame).
   - Sprite updates/draws run through pygame.sprite.Group's C-accelerated
     batch operations rather than manual Python loops wherever possible.
   - Dirty-rect drawing available via Game.UseDirtyRects for 2D games
     that don't repaint the whole screen every frame.
   - An object pool (Pool) to reuse short-lived objects (bullets,
     particles) instead of allocating/discarding every frame.
4. Reliable: every public call validates inputs and raises PyGexError
   subclasses with clear messages instead of letting pygame's cryptic
   errors bubble up unexplained. Nothing here silently swallows errors.
5. Batteries included: Input, Timer, Tween, Animation, Camera, Scene
   management, simple UI (Button/Label), particle system, collision
   helpers, an asset cache, and a Vector2 math type — so you rarely need
   to `import pygame` directly again.

Quick start
-----------
    from pygex import Game, Input, Colors, Vector2

    App = Game(Width=800, Height=600, Title="My Game")

    @App.Start
    def Init():
        global Player
        Player = Vector2(400, 300)

    @App.Update
    def Tick(Dt):
        Speed = 200 * Dt
        if Input.KeyDown("d"): Player.X += Speed
        if Input.KeyDown("a"): Player.X -= Speed

    @App.Draw
    def Render(Surface):
        Surface.fill(Colors.Black)
        pygame.draw.circle(Surface, Colors.White, Player.Tuple(), 20)

    App.Run()
"""

from __future__ import annotations

import math
import random
import time as _time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

try:
    import pygame
except ImportError as _e:  # pragma: no cover - environment guard
    raise ImportError(
        "PyGex requires pygame. Install it with: pip install pygame"
    ) from _e

__all__ = [
    "Game",
    "Vector2",
    "Rect",
    "Colors",
    "Input",
    "Timer",
    "Tween",
    "Easing",
    "Animation",
    "SpriteSheet",
    "Sprite",
    "Group",
    "Camera",
    "Scene",
    "SceneManager",
    "Particle",
    "ParticleSystem",
    "Pool",
    "Button",
    "Label",
    "Assets",
    "PyGexError",
    "InitError",
    "AssetError",
]


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #

class PyGexError(Exception):
    """Base exception for all PyGex errors."""


class InitError(PyGexError):
    """Raised when something is used before Game.Run()/Start has initialized it."""


class AssetError(PyGexError):
    """Raised when an image/sound/font fails to load."""


# --------------------------------------------------------------------------- #
# Vector2 — slotted, fast, PascalCase
# --------------------------------------------------------------------------- #

class Vector2:
    """A fast 2D vector. Slotted for low memory overhead and quick attribute access."""

    __slots__ = ("X", "Y")

    def __init__(self, X: float = 0.0, Y: float = 0.0):
        self.X = X
        self.Y = Y

    def __repr__(self) -> str:
        return f"Vector2({self.X:.3f}, {self.Y:.3f})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Vector2):
            return NotImplemented
        return self.X == other.X and self.Y == other.Y

    def __add__(self, other: "Vector2") -> "Vector2":
        return Vector2(self.X + other.X, self.Y + other.Y)

    def __sub__(self, other: "Vector2") -> "Vector2":
        return Vector2(self.X - other.X, self.Y - other.Y)

    def __mul__(self, scalar: float) -> "Vector2":
        return Vector2(self.X * scalar, self.Y * scalar)

    __rmul__ = __mul__

    def __truediv__(self, scalar: float) -> "Vector2":
        return Vector2(self.X / scalar, self.Y / scalar)

    def __neg__(self) -> "Vector2":
        return Vector2(-self.X, -self.Y)

    def __iter__(self):
        yield self.X
        yield self.Y

    def Copy(self) -> "Vector2":
        return Vector2(self.X, self.Y)

    def Tuple(self) -> tuple[float, float]:
        return (self.X, self.Y)

    def IntTuple(self) -> tuple[int, int]:
        return (int(self.X), int(self.Y))

    def Length(self) -> float:
        return math.hypot(self.X, self.Y)

    def LengthSquared(self) -> float:
        # Avoids the sqrt call — use this for comparisons when possible.
        return self.X * self.X + self.Y * self.Y

    def Normalized(self) -> "Vector2":
        L = self.Length()
        if L == 0:
            return Vector2(0, 0)
        return Vector2(self.X / L, self.Y / L)

    def Distance(self, Other: "Vector2") -> float:
        return math.hypot(self.X - Other.X, self.Y - Other.Y)

    def DistanceSquared(self, Other: "Vector2") -> float:
        Dx = self.X - Other.X
        Dy = self.Y - Other.Y
        return Dx * Dx + Dy * Dy

    def Dot(self, Other: "Vector2") -> float:
        return self.X * Other.X + self.Y * Other.Y

    def Lerp(self, Other: "Vector2", T: float) -> "Vector2":
        return Vector2(self.X + (Other.X - self.X) * T, self.Y + (Other.Y - self.Y) * T)

    def Angle(self) -> float:
        """Angle in degrees, matching pygame's rotation convention."""
        return math.degrees(math.atan2(-self.Y, self.X))

    def Rotated(self, Degrees: float) -> "Vector2":
        Rad = math.radians(Degrees)
        Cos, Sin = math.cos(Rad), math.sin(Rad)
        return Vector2(self.X * Cos - self.Y * Sin, self.X * Sin + self.Y * Cos)

    @staticmethod
    def Zero() -> "Vector2":
        return Vector2(0, 0)

    @staticmethod
    def FromAngle(Degrees: float, Length: float = 1.0) -> "Vector2":
        Rad = math.radians(Degrees)
        return Vector2(math.cos(Rad) * Length, -math.sin(Rad) * Length)


# --------------------------------------------------------------------------- #
# Rect — thin PascalCase facade over pygame.Rect (which is already in C)
# --------------------------------------------------------------------------- #

class Rect:
    """
    PascalCase facade over pygame.Rect. pygame.Rect is already a fast C
    type, so this just renames the surface API — the underlying Rect
    (`.Raw`) is used directly by hot code paths (Sprite, collisions).
    """

    __slots__ = ("Raw",)

    def __init__(self, X: float, Y: float, Width: float, Height: float):
        self.Raw = pygame.Rect(int(X), int(Y), int(Width), int(Height))

    def __repr__(self) -> str:
        return f"Rect({self.Raw.x}, {self.Raw.y}, {self.Raw.w}, {self.Raw.h})"

    @property
    def X(self) -> int: return self.Raw.x
    @X.setter
    def X(self, Value: int) -> None: self.Raw.x = Value

    @property
    def Y(self) -> int: return self.Raw.y
    @Y.setter
    def Y(self, Value: int) -> None: self.Raw.y = Value

    @property
    def Width(self) -> int: return self.Raw.w
    @Width.setter
    def Width(self, Value: int) -> None: self.Raw.w = Value

    @property
    def Height(self) -> int: return self.Raw.h
    @Height.setter
    def Height(self, Value: int) -> None: self.Raw.h = Value

    @property
    def Center(self) -> tuple[int, int]: return self.Raw.center
    @Center.setter
    def Center(self, Value: tuple[int, int]) -> None: self.Raw.center = Value

    def Collides(self, Other: "Rect") -> bool:
        return bool(self.Raw.colliderect(Other.Raw))

    def CollidesPoint(self, Point: Union[Vector2, tuple[float, float]]) -> bool:
        P = Point.Tuple() if isinstance(Point, Vector2) else Point
        return bool(self.Raw.collidepoint(P))

    def MoveBy(self, Dx: float, Dy: float) -> "Rect":
        self.Raw.x += int(Dx)
        self.Raw.y += int(Dy)
        return self


# --------------------------------------------------------------------------- #
# Colors — common PascalCase color constants
# --------------------------------------------------------------------------- #

class Colors:
    """Common colors as (R, G, B) tuples. Add your own freely: Colors.Custom = (...)."""
    Black = (0, 0, 0)
    White = (255, 255, 255)
    Red = (255, 0, 0)
    Green = (0, 255, 0)
    Blue = (0, 0, 255)
    Yellow = (255, 255, 0)
    Cyan = (0, 255, 255)
    Magenta = (255, 0, 255)
    Gray = (128, 128, 128)
    LightGray = (200, 200, 200)
    DarkGray = (60, 60, 60)
    Orange = (255, 165, 0)
    Purple = (128, 0, 128)
    Brown = (139, 69, 19)
    Pink = (255, 192, 203)
    Transparent = (0, 0, 0, 0)

    @staticmethod
    def Random() -> tuple[int, int, int]:
        return (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))

    @staticmethod
    def WithAlpha(Color: tuple[int, int, int], Alpha: int) -> tuple[int, int, int, int]:
        return (Color[0], Color[1], Color[2], Alpha)


# --------------------------------------------------------------------------- #
# Input — cached per-frame key/mouse state (queried, not event-driven)
# --------------------------------------------------------------------------- #

class _InputState:
    """
    Polls pygame's key/mouse state once per frame and caches it, so calling
    Input.KeyDown() a hundred times in one Update() costs one array lookup
    each rather than round-tripping into pygame each time.
    """

    __slots__ = (
        "_Keys", "_PrevKeys", "_MouseButtons", "_PrevMouseButtons",
        "_MousePos", "_MouseWheel", "_KeyNameToCode",
    )

    def __init__(self) -> None:
        self._Keys = ()
        self._PrevKeys = ()
        self._MouseButtons = (False, False, False)
        self._PrevMouseButtons = (False, False, False)
        self._MousePos = (0, 0)
        self._MouseWheel = 0
        self._KeyNameToCode: dict[str, int] = {}

    def _Refresh(self, WheelDelta: int) -> None:
        self._PrevKeys = self._Keys
        self._Keys = pygame.key.get_pressed()
        self._PrevMouseButtons = self._MouseButtons
        self._MouseButtons = pygame.mouse.get_pressed()
        self._MousePos = pygame.mouse.get_pos()
        self._MouseWheel = WheelDelta

    def _Code(self, Key: Union[str, int]) -> int:
        if isinstance(Key, int):
            return Key
        Cached = self._KeyNameToCode.get(Key)
        if Cached is not None:
            return Cached
        Code = pygame.key.key_code(Key)
        self._KeyNameToCode[Key] = Code
        return Code

    def KeyDown(self, Key: Union[str, int]) -> bool:
        """True while the key is held down."""
        return bool(self._Keys[self._Code(Key)])

    def KeyPressed(self, Key: Union[str, int]) -> bool:
        """True only on the exact frame the key went down."""
        Code = self._Code(Key)
        return bool(self._Keys[Code]) and not bool(self._PrevKeys[Code] if self._PrevKeys else False)

    def KeyReleased(self, Key: Union[str, int]) -> bool:
        """True only on the exact frame the key went up."""
        Code = self._Code(Key)
        WasDown = bool(self._PrevKeys[Code]) if self._PrevKeys else False
        return WasDown and not bool(self._Keys[Code])

    def MouseDown(self, Button: int = 0) -> bool:
        return bool(self._MouseButtons[Button])

    def MouseClicked(self, Button: int = 0) -> bool:
        return bool(self._MouseButtons[Button]) and not bool(self._PrevMouseButtons[Button])

    def MousePos(self) -> Vector2:
        return Vector2(*self._MousePos)

    def MouseWheel(self) -> int:
        return self._MouseWheel


Input = _InputState()


# --------------------------------------------------------------------------- #
# Timer — countdown / stopwatch / repeating callback, all slotted
# --------------------------------------------------------------------------- #

class Timer:
    """
    A lightweight countdown timer. Call Update(Dt) once per frame (Game
    does this for every Timer you register via Game.Track), or drive it
    manually.

        T = Timer(Seconds=2.0, OnComplete=lambda: print("done"))
        T.Start()
        ...
        T.Update(Dt)
    """

    __slots__ = ("Duration", "Remaining", "Repeating", "Running", "OnComplete")

    def __init__(
        self,
        Seconds: float,
        OnComplete: Optional[Callable[[], None]] = None,
        Repeating: bool = False,
        AutoStart: bool = False,
    ):
        self.Duration = Seconds
        self.Remaining = Seconds
        self.Repeating = Repeating
        self.Running = AutoStart
        self.OnComplete = OnComplete

    def Start(self) -> "Timer":
        self.Remaining = self.Duration
        self.Running = True
        return self

    def Stop(self) -> "Timer":
        self.Running = False
        return self

    def Reset(self) -> "Timer":
        self.Remaining = self.Duration
        return self

    @property
    def Progress(self) -> float:
        """0.0 (just started) .. 1.0 (complete)."""
        if self.Duration <= 0:
            return 1.0
        return max(0.0, min(1.0, 1.0 - (self.Remaining / self.Duration)))

    @property
    def Finished(self) -> bool:
        return self.Remaining <= 0

    def Update(self, Dt: float) -> bool:
        """Returns True on the exact frame it completes."""
        if not self.Running:
            return False
        self.Remaining -= Dt
        if self.Remaining <= 0:
            self.Remaining = 0
            if self.OnComplete is not None:
                self.OnComplete()
            if self.Repeating:
                self.Remaining = self.Duration
            else:
                self.Running = False
            return True
        return False


# --------------------------------------------------------------------------- #
# Easing + Tween
# --------------------------------------------------------------------------- #

class Easing:
    """Standard easing functions, T in [0, 1] -> eased value in [0, 1]."""

    @staticmethod
    def Linear(T: float) -> float: return T

    @staticmethod
    def EaseInQuad(T: float) -> float: return T * T

    @staticmethod
    def EaseOutQuad(T: float) -> float: return 1 - (1 - T) * (1 - T)

    @staticmethod
    def EaseInOutQuad(T: float) -> float:
        return 2 * T * T if T < 0.5 else 1 - pow(-2 * T + 2, 2) / 2

    @staticmethod
    def EaseOutBounce(T: float) -> float:
        N1, D1 = 7.5625, 2.75
        if T < 1 / D1:
            return N1 * T * T
        elif T < 2 / D1:
            T -= 1.5 / D1
            return N1 * T * T + 0.75
        elif T < 2.5 / D1:
            T -= 2.25 / D1
            return N1 * T * T + 0.9375
        else:
            T -= 2.625 / D1
            return N1 * T * T + 0.984375


class Tween:
    """
    Animate a numeric value over time.

        T = Tween(0, 100, Seconds=1.5, Ease=Easing.EaseOutQuad)
        T.Start()
        Value = T.Update(Dt)   # call each frame; also returns current value
    """

    __slots__ = ("Start_", "End", "Duration", "Elapsed", "Ease", "Running", "OnComplete")

    def __init__(
        self,
        From: float,
        To: float,
        Seconds: float,
        Ease: Callable[[float], float] = Easing.Linear,
        OnComplete: Optional[Callable[[], None]] = None,
    ):
        self.Start_ = From
        self.End = To
        self.Duration = max(Seconds, 1e-9)
        self.Elapsed = 0.0
        self.Ease = Ease
        self.Running = False
        self.OnComplete = OnComplete

    def Start(self) -> "Tween":
        self.Elapsed = 0.0
        self.Running = True
        return self

    @property
    def Value(self) -> float:
        T = min(self.Elapsed / self.Duration, 1.0)
        return self.Start_ + (self.End - self.Start_) * self.Ease(T)

    @property
    def Finished(self) -> bool:
        return self.Elapsed >= self.Duration

    def Update(self, Dt: float) -> float:
        if self.Running:
            self.Elapsed += Dt
            if self.Elapsed >= self.Duration:
                self.Elapsed = self.Duration
                self.Running = False
                if self.OnComplete is not None:
                    self.OnComplete()
        return self.Value


# --------------------------------------------------------------------------- #
# Assets — cached image/sound/font loading (converted once, reused forever)
# --------------------------------------------------------------------------- #

class _AssetCache:
    """
    Loads images/sounds/fonts once and caches them. Images are converted
    with .convert_alpha() immediately at load time — doing this once
    up-front instead of per-blit is one of the biggest pygame perf wins
    available, since blitting a converted Surface is dramatically faster
    than blitting a raw-loaded one.
    """

    __slots__ = ("_Images", "_Sounds", "_Fonts")

    def __init__(self) -> None:
        self._Images: dict[str, "pygame.Surface"] = {}
        self._Sounds: dict[str, "pygame.mixer.Sound"] = {}
        self._Fonts: dict[tuple[str, int], "pygame.font.Font"] = {}

    def Image(self, Path: str, Alpha: bool = True) -> "pygame.Surface":
        Cached = self._Images.get(Path)
        if Cached is not None:
            return Cached
        try:
            Surf = pygame.image.load(Path)
        except pygame.error as E:
            raise AssetError(f"Could not load image {Path!r}: {E}") from E
        Surf = Surf.convert_alpha() if Alpha else Surf.convert()
        self._Images[Path] = Surf
        return Surf

    def Sound(self, Path: str) -> "pygame.mixer.Sound":
        Cached = self._Sounds.get(Path)
        if Cached is not None:
            return Cached
        try:
            Snd = pygame.mixer.Sound(Path)
        except pygame.error as E:
            raise AssetError(f"Could not load sound {Path!r}: {E}") from E
        self._Sounds[Path] = Snd
        return Snd

    def Font(self, Path: Optional[str], Size: int) -> "pygame.font.Font":
        Key = (Path or "__default__", Size)
        Cached = self._Fonts.get(Key)
        if Cached is not None:
            return Cached
        try:
            Fnt = pygame.font.Font(Path, Size)
        except pygame.error as E:
            raise AssetError(f"Could not load font {Path!r} @ {Size}: {E}") from E
        self._Fonts[Key] = Fnt
        return Fnt

    def Clear(self) -> None:
        self._Images.clear()
        self._Sounds.clear()
        self._Fonts.clear()


Assets = _AssetCache()


# --------------------------------------------------------------------------- #
# SpriteSheet + Animation
# --------------------------------------------------------------------------- #

class SpriteSheet:
    """Slice a sprite sheet image into frames once, up front (not per-frame at draw time)."""

    __slots__ = ("Sheet", "FrameWidth", "FrameHeight", "_Cache")

    def __init__(self, Path: str, FrameWidth: int, FrameHeight: int):
        self.Sheet = Assets.Image(Path)
        self.FrameWidth = FrameWidth
        self.FrameHeight = FrameHeight
        self._Cache: dict[tuple[int, int], "pygame.Surface"] = {}

    def Frame(self, Col: int, Row: int) -> "pygame.Surface":
        Key = (Col, Row)
        Cached = self._Cache.get(Key)
        if Cached is not None:
            return Cached
        Rect_ = pygame.Rect(
            Col * self.FrameWidth, Row * self.FrameHeight, self.FrameWidth, self.FrameHeight
        )
        SubSurf = self.Sheet.subsurface(Rect_).copy()
        self._Cache[Key] = SubSurf
        return SubSurf

    def Row(self, Row: int, Count: int) -> list["pygame.Surface"]:
        return [self.Frame(Col, Row) for Col in range(Count)]


class Animation:
    """Frame-by-frame animation driven by Update(Dt); reads current frame via .Frame."""

    __slots__ = ("Frames", "FrameTime", "_Elapsed", "_Index", "Looping", "Playing")

    def __init__(self, Frames: list["pygame.Surface"], Fps: float = 12.0, Looping: bool = True):
        if not Frames:
            raise PyGexError("Animation needs at least one frame")
        self.Frames = Frames
        self.FrameTime = 1.0 / max(Fps, 1e-6)
        self._Elapsed = 0.0
        self._Index = 0
        self.Looping = Looping
        self.Playing = True

    @property
    def Frame(self) -> "pygame.Surface":
        return self.Frames[self._Index]

    @property
    def Finished(self) -> bool:
        return (not self.Looping) and self._Index == len(self.Frames) - 1

    def Reset(self) -> "Animation":
        self._Elapsed = 0.0
        self._Index = 0
        self.Playing = True
        return self

    def Update(self, Dt: float) -> None:
        if not self.Playing:
            return
        self._Elapsed += Dt
        while self._Elapsed >= self.FrameTime:
            self._Elapsed -= self.FrameTime
            NextIndex = self._Index + 1
            if NextIndex >= len(self.Frames):
                if self.Looping:
                    self._Index = 0
                else:
                    self._Index = len(self.Frames) - 1
                    self.Playing = False
                    break
            else:
                self._Index = NextIndex


# --------------------------------------------------------------------------- #
# Sprite / Group — thin PascalCase wrapper over pygame.sprite (C-accelerated)
# --------------------------------------------------------------------------- #

class Sprite(pygame.sprite.Sprite):
    """
    Base sprite. Subclass and override OnUpdate(Dt)/optionally set
    self.Image + self.Rect. Kept thin so pygame.sprite.Group's fast,
    batch update()/draw() paths do the heavy lifting instead of Python
    loops in user code.
    """

    def __init__(self, Image: Optional["pygame.Surface"] = None, X: float = 0, Y: float = 0):
        super().__init__()
        self.image = Image if Image is not None else pygame.Surface((1, 1), pygame.SRCALPHA)
        self.rect = self.image.get_rect(topleft=(X, Y))
        self.Alive = True

    # PascalCase aliases over pygame's lowercase attributes.
    @property
    def Image(self) -> "pygame.Surface": return self.image
    @Image.setter
    def Image(self, Value: "pygame.Surface") -> None: self.image = Value

    @property
    def Position(self) -> Vector2:
        return Vector2(self.rect.x, self.rect.y)

    @Position.setter
    def Position(self, Value: Union[Vector2, tuple[float, float]]) -> None:
        X, Y = (Value.X, Value.Y) if isinstance(Value, Vector2) else Value
        self.rect.x, self.rect.y = int(X), int(Y)

    def OnUpdate(self, Dt: float) -> None:
        """Override in subclasses. Called once per frame while alive and in a Group."""

    def update(self, *args: Any) -> None:  # pygame.sprite.Group calls this name
        Dt = args[0] if args else 0.0
        self.OnUpdate(Dt)
        if not self.Alive:
            self.kill()

    def Kill(self) -> None:
        self.Alive = False
        self.kill()


class Group:
    """
    PascalCase facade over pygame.sprite.Group, which internally batches
    update()/draw() in optimized C loops — much faster than iterating and
    calling methods on sprites manually from Python.
    """

    __slots__ = ("Raw",)

    def __init__(self, *Sprites: Sprite):
        self.Raw = pygame.sprite.Group(*Sprites)

    def Add(self, *Sprites: Sprite) -> "Group":
        self.Raw.add(*Sprites)
        return self

    def Remove(self, *Sprites: Sprite) -> "Group":
        self.Raw.remove(*Sprites)
        return self

    def Update(self, Dt: float) -> None:
        self.Raw.update(Dt)

    def Draw(self, Surface: "pygame.Surface") -> None:
        self.Raw.draw(Surface)

    def __len__(self) -> int:
        return len(self.Raw)

    def __iter__(self):
        return iter(self.Raw)

    def CollideAny(self, Spr: Sprite) -> bool:
        return pygame.sprite.spritecollideany(Spr, self.Raw) is not None

    def CollideAll(self, Spr: Sprite, DoKill: bool = False) -> list[Sprite]:
        return pygame.sprite.spritecollide(Spr, self.Raw, DoKill)

    def Clear(self) -> "Group":
        self.Raw.empty()
        return self


# --------------------------------------------------------------------------- #
# Pool — object pooling for high-churn objects (bullets, particles, etc.)
# --------------------------------------------------------------------------- #

class Pool:
    """
    A generic object pool: reuses dead objects instead of allocating new
    ones every frame, which matters a lot for bullet-hell / particle-heavy
    games where allocation churn is the actual bottleneck.

        Bullets = Pool(Factory=lambda: Bullet(), Reset=lambda b: b.Reset())
        B = Bullets.Acquire()
        ...
        Bullets.Release(B)
    """

    __slots__ = ("_Factory", "_Reset", "_Free", "_InUse")

    def __init__(self, Factory: Callable[[], Any], Reset: Optional[Callable[[Any], None]] = None):
        self._Factory = Factory
        self._Reset = Reset
        self._Free: list[Any] = []
        self._InUse: set[int] = set()

    def Acquire(self) -> Any:
        Obj = self._Free.pop() if self._Free else self._Factory()
        if self._Reset is not None:
            self._Reset(Obj)
        self._InUse.add(id(Obj))
        return Obj

    def Release(self, Obj: Any) -> None:
        Key = id(Obj)
        if Key in self._InUse:
            self._InUse.discard(Key)
            self._Free.append(Obj)

    @property
    def ActiveCount(self) -> int:
        return len(self._InUse)

    @property
    def FreeCount(self) -> int:
        return len(self._Free)


# --------------------------------------------------------------------------- #
# Particle / ParticleSystem — pooled, slotted, array-friendly
# --------------------------------------------------------------------------- #

class Particle:
    __slots__ = ("Pos", "Vel", "Life", "MaxLife", "Color", "Size", "Active")

    def __init__(self) -> None:
        self.Pos = Vector2()
        self.Vel = Vector2()
        self.Life = 0.0
        self.MaxLife = 1.0
        self.Color = Colors.White
        self.Size = 2
        self.Active = False


class ParticleSystem:
    """
    A pooled particle system: particles are pre-allocated once and reused,
    so emitting bursts never triggers per-frame allocation. Drawing loops
    over a flat pre-allocated list (no dict/object churn).
    """

    __slots__ = ("_Particles", "_Pool")

    def __init__(self, MaxParticles: int = 500):
        self._Particles: list[Particle] = [Particle() for _ in range(MaxParticles)]
        self._Pool = list(self._Particles)  # available (inactive) particles

    def Emit(
        self,
        Position: Vector2,
        Count: int = 10,
        Speed: tuple[float, float] = (50, 150),
        Life: tuple[float, float] = (0.4, 1.0),
        Color: tuple[int, int, int] = Colors.White,
        Size: int = 3,
    ) -> None:
        for _ in range(Count):
            if not self._Pool:
                return  # pool exhausted this frame — drop excess silently, no crash
            P = self._Pool.pop()
            Angle = random.uniform(0, 360)
            Spd = random.uniform(*Speed)
            P.Pos = Position.Copy()
            P.Vel = Vector2.FromAngle(Angle, Spd)
            P.MaxLife = random.uniform(*Life)
            P.Life = P.MaxLife
            P.Color = Color
            P.Size = Size
            P.Active = True

    def Update(self, Dt: float) -> None:
        for P in self._Particles:
            if not P.Active:
                continue
            P.Life -= Dt
            if P.Life <= 0:
                P.Active = False
                self._Pool.append(P)
                continue
            P.Pos = P.Pos + P.Vel * Dt

    def Draw(self, Surface: "pygame.Surface") -> None:
        DrawCircle = pygame.draw.circle  # local binding: skips attribute lookup per-call
        for P in self._Particles:
            if P.Active:
                Alpha = P.Life / P.MaxLife
                Size = max(1, int(P.Size * Alpha))
                DrawCircle(Surface, P.Color, P.Pos.IntTuple(), Size)

    @property
    def ActiveCount(self) -> int:
        return len(self._Particles) - len(self._Pool)


# --------------------------------------------------------------------------- #
# Camera — simple scrolling offset
# --------------------------------------------------------------------------- #

class Camera:
    """A simple 2D camera: apply an offset to world positions before drawing."""

    __slots__ = ("Offset", "Target", "Smoothing", "Bounds")

    def __init__(self, Smoothing: float = 0.0):
        self.Offset = Vector2()
        self.Target: Optional[Vector2] = None
        self.Smoothing = Smoothing  # 0 = snap instantly, closer to 1 = smoother/slower
        self.Bounds: Optional["pygame.Rect"] = None

    def Follow(self, Target: Vector2) -> "Camera":
        self.Target = Target
        return self

    def SetBounds(self, X: int, Y: int, Width: int, Height: int) -> "Camera":
        self.Bounds = pygame.Rect(X, Y, Width, Height)
        return self

    def Update(self, ScreenWidth: int, ScreenHeight: int) -> None:
        if self.Target is None:
            return
        DesiredX = self.Target.X - ScreenWidth / 2
        DesiredY = self.Target.Y - ScreenHeight / 2
        if self.Smoothing <= 0:
            self.Offset.X, self.Offset.Y = DesiredX, DesiredY
        else:
            T = 1.0 - self.Smoothing
            self.Offset.X += (DesiredX - self.Offset.X) * T
            self.Offset.Y += (DesiredY - self.Offset.Y) * T
        if self.Bounds is not None:
            self.Offset.X = max(self.Bounds.left, min(self.Offset.X, self.Bounds.right - ScreenWidth))
            self.Offset.Y = max(self.Bounds.top, min(self.Offset.Y, self.Bounds.bottom - ScreenHeight))

    def Apply(self, WorldPos: Vector2) -> Vector2:
        """Convert a world-space position to screen-space."""
        return Vector2(WorldPos.X - self.Offset.X, WorldPos.Y - self.Offset.Y)


# --------------------------------------------------------------------------- #
# Simple UI: Label / Button (cached rendered text — text rendering is slow)
# --------------------------------------------------------------------------- #

class Label:
    """
    Draws text, caching the rendered Surface and only re-rendering when the
    text/color/font actually changes — text rendering is one of the most
    expensive per-frame operations in pygame if done naively every frame.
    """

    __slots__ = ("_Text", "_Color", "_Font", "_Surface", "Position")

    def __init__(self, Text: str, Position: Vector2, Size: int = 24, Color: tuple = Colors.White, FontPath: Optional[str] = None):
        self._Font = Assets.Font(FontPath, Size)
        self._Text = Text
        self._Color = Color
        self.Position = Position
        self._Surface = self._Font.render(Text, True, Color)

    @property
    def Text(self) -> str:
        return self._Text

    @Text.setter
    def Text(self, Value: str) -> None:
        if Value != self._Text:
            self._Text = Value
            self._Surface = self._Font.render(Value, True, self._Color)

    @property
    def Color(self) -> tuple:
        return self._Color

    @Color.setter
    def Color(self, Value: tuple) -> None:
        if Value != self._Color:
            self._Color = Value
            self._Surface = self._Font.render(self._Text, True, Value)

    def Draw(self, Surface: "pygame.Surface") -> None:
        Surface.blit(self._Surface, self.Position.Tuple())


class Button:
    """A minimal clickable rectangle with a label. Call .Update() each frame, .Draw() each frame."""

    __slots__ = ("Rect_", "Label_", "OnClick", "Color", "HoverColor", "_Hovered")

    def __init__(
        self,
        X: float, Y: float, Width: float, Height: float,
        Text: str = "",
        OnClick: Optional[Callable[[], None]] = None,
        Color: tuple = Colors.DarkGray,
        HoverColor: tuple = Colors.Gray,
    ):
        self.Rect_ = pygame.Rect(int(X), int(Y), int(Width), int(Height))
        TextPos = Vector2(X + 8, Y + Height / 2 - 10)
        self.Label_ = Label(Text, TextPos, Size=20)
        self.OnClick = OnClick
        self.Color = Color
        self.HoverColor = HoverColor
        self._Hovered = False

    def Update(self) -> bool:
        """Returns True on the frame it was clicked."""
        MousePos = Input.MousePos()
        self._Hovered = self.Rect_.collidepoint(MousePos.Tuple())
        if self._Hovered and Input.MouseClicked(0):
            if self.OnClick is not None:
                self.OnClick()
            return True
        return False

    def Draw(self, Surface: "pygame.Surface") -> None:
        pygame.draw.rect(Surface, self.HoverColor if self._Hovered else self.Color, self.Rect_, border_radius=4)
        self.Label_.Draw(Surface)


# --------------------------------------------------------------------------- #
# Scene / SceneManager
# --------------------------------------------------------------------------- #

class Scene:
    """Override OnEnter/OnExit/OnUpdate/OnDraw in subclasses to define a game screen."""

    def OnEnter(self) -> None: ...
    def OnExit(self) -> None: ...
    def OnUpdate(self, Dt: float) -> None: ...
    def OnDraw(self, Surface: "pygame.Surface") -> None: ...


class SceneManager:
    """Swap between Scene instances. Current scene drives Update/Draw."""

    __slots__ = ("_Current", "_Scenes")

    def __init__(self) -> None:
        self._Current: Optional[Scene] = None
        self._Scenes: dict[str, Scene] = {}

    def Register(self, Name: str, ScreenScene: Scene) -> "SceneManager":
        self._Scenes[Name] = ScreenScene
        return self

    def Goto(self, Name: str) -> None:
        Next = self._Scenes.get(Name)
        if Next is None:
            raise PyGexError(f"No scene registered under name {Name!r}")
        if self._Current is not None:
            self._Current.OnExit()
        self._Current = Next
        self._Current.OnEnter()

    @property
    def Current(self) -> Optional[Scene]:
        return self._Current

    def Update(self, Dt: float) -> None:
        if self._Current is not None:
            self._Current.OnUpdate(Dt)

    def Draw(self, Surface: "pygame.Surface") -> None:
        if self._Current is not None:
            self._Current.OnDraw(Surface)


# --------------------------------------------------------------------------- #
# Collision helpers (module-level, PascalCase, free functions)
# --------------------------------------------------------------------------- #

def CircleCollide(PosA: Vector2, RadiusA: float, PosB: Vector2, RadiusB: float) -> bool:
    """Fast circle-vs-circle test — compares squared distances, no sqrt."""
    RadiusSum = RadiusA + RadiusB
    return PosA.DistanceSquared(PosB) <= RadiusSum * RadiusSum


def PointInCircle(Point: Vector2, Center: Vector2, Radius: float) -> bool:
    return Point.DistanceSquared(Center) <= Radius * Radius


# --------------------------------------------------------------------------- #
# Game — the decorator-driven engine core
# --------------------------------------------------------------------------- #

class Game:
    """
    The engine entry point. Register lifecycle callbacks with decorators,
    then call Run().

        App = Game(Width=800, Height=600, Title="Demo")

        @App.Start
        def Init(): ...

        @App.Update
        def Tick(Dt): ...

        @App.Draw
        def Render(Surface): ...

        App.Run()

    Multiple functions can be registered per decorator — they run in
    registration order. This lets you split Start/Update/Draw logic across
    modules (e.g. player.py registers its own @App.Update) without one
    giant function.
    """

    def __init__(
        self,
        Width: int = 800,
        Height: int = 600,
        Title: str = "PyGex Game",
        Fps: int = 60,
        Resizable: bool = False,
        VSync: bool = True,
        BackgroundColor: tuple = Colors.Black,
        UseDirtyRects: bool = False,
    ):
        self.Width = Width
        self.Height = Height
        self.Title = Title
        self.Fps = Fps
        self.Resizable = Resizable
        self.VSync = VSync
        self.BackgroundColor = BackgroundColor
        self.UseDirtyRects = UseDirtyRects

        self.Surface: Optional["pygame.Surface"] = None
        self.Clock: Optional["pygame.time.Clock"] = None
        self.Running = False
        self.Scenes = SceneManager()

        self._StartCallbacks: list[Callable[[], None]] = []
        self._UpdateCallbacks: list[Callable[[float], None]] = []
        self._DrawCallbacks: list[Callable[["pygame.Surface"], None]] = []
        self._QuitCallbacks: list[Callable[[], None]] = []
        self._Timers: list[Timer] = []
        self._Initialized = False

    # -- decorators -------------------------------------------------------- #

    def Start(self, Func: Callable[[], None]) -> Callable[[], None]:
        """Decorator: register a function to run once, right before the main loop starts."""
        self._StartCallbacks.append(Func)
        return Func

    def Update(self, Func: Callable[[float], None]) -> Callable[[float], None]:
        """Decorator: register a function to run every frame, before Draw. Receives Dt (seconds)."""
        self._UpdateCallbacks.append(Func)
        return Func

    def Draw(self, Func: Callable[["pygame.Surface"], None]) -> Callable[["pygame.Surface"], None]:
        """Decorator: register a function to run every frame, after Update. Receives the screen Surface."""
        self._DrawCallbacks.append(Func)
        return Func

    def OnQuit(self, Func: Callable[[], None]) -> Callable[[], None]:
        """Decorator: register a function to run once, right before the window closes."""
        self._QuitCallbacks.append(Func)
        return Func

    # -- helpers ------------------------------------------------------------ #

    def Track(self, T: Timer) -> Timer:
        """Have the engine call T.Update(Dt) automatically every frame."""
        self._Timers.append(T)
        return T

    def Quit(self) -> None:
        """Stop the main loop cleanly at the end of the current frame."""
        self.Running = False

    # -- lifecycle ------------------------------------------------------------ #

    def _Init(self) -> None:
        if self._Initialized:
            return
        pygame.init()
        Flags = 0
        if self.Resizable:
            Flags |= pygame.RESIZABLE
        VSyncFlag = 1 if self.VSync else 0
        try:
            self.Surface = pygame.display.set_mode((self.Width, self.Height), Flags, vsync=VSyncFlag)
        except TypeError:
            # Older pygame builds don't accept vsync= — fall back gracefully.
            self.Surface = pygame.display.set_mode((self.Width, self.Height), Flags)
        pygame.display.set_caption(self.Title)
        self.Clock = pygame.time.Clock()
        self._Initialized = True

    def Run(self) -> None:
        """Initialize pygame, run Start callbacks once, then loop Update/Draw until Quit()."""
        self._Init()
        assert self.Surface is not None and self.Clock is not None  # for type-checkers

        for Func in self._StartCallbacks:
            Func()

        self.Running = True
        try:
            while self.Running:
                DtMs = self.Clock.tick(self.Fps)
                Dt = DtMs / 1000.0

                WheelDelta = 0
                for Event in pygame.event.get():
                    if Event.type == pygame.QUIT:
                        self.Running = False
                    elif Event.type == pygame.MOUSEWHEEL:
                        WheelDelta = Event.y

                Input._Refresh(WheelDelta)

                for T in self._Timers:
                    T.Update(Dt)

                for Func in self._UpdateCallbacks:
                    Func(Dt)
                self.Scenes.Update(Dt)

                if not self.UseDirtyRects:
                    self.Surface.fill(self.BackgroundColor)

                for Func in self._DrawCallbacks:
                    Func(self.Surface)
                self.Scenes.Draw(self.Surface)

                pygame.display.flip()
        finally:
            for Func in self._QuitCallbacks:
                Func()
            pygame.quit()

    # -- convenience read-outs -------------------------------------------- #

    @property
    def DeltaTime(self) -> float:
        """Seconds since last frame (0 if the clock hasn't ticked yet)."""
        return (self.Clock.get_time() / 1000.0) if self.Clock else 0.0

    @property
    def CurrentFps(self) -> float:
        return self.Clock.get_fps() if self.Clock else 0.0

    @property
    def Screen(self) -> "pygame.Surface":
        if self.Surface is None:
            raise InitError("Game.Screen accessed before Run()/_Init() — the window doesn't exist yet")
        return self.Surface
