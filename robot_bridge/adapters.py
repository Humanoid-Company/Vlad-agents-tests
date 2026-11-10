"""
Адаптери роботів. Кожен реалізує однаковий інтерфейс:
  move(vx, vy, yaw)  — швидкості, м/с і рад/с (вже обрізані bridge'ем)
  stop()
  action(name)
  capabilities       — що надіслати релею при реєстрації
  state()            — dict для статусу на сайті
"""


class RobotAdapter:
    kind = "generic"
    actions: list[str] = []
    max_vx = 0.5
    max_vy = 0.3
    max_yaw = 0.8

    def connect(self): ...
    def move(self, vx: float, vy: float, yaw: float): raise NotImplementedError
    def stop(self): raise NotImplementedError
    def action(self, name: str): raise NotImplementedError
    def state(self) -> dict: return {}

    def capabilities(self) -> dict:
        return {
            "kind": self.kind,
            "actions": self.actions,
            "max_speed": {"vx": self.max_vx, "vy": self.max_vy, "yaw": self.max_yaw},
        }


# ------------------------------------------------------------------
class MockAdapter(RobotAdapter):
    """Емулятор — для перевірки без робота. Друкує команди в консоль."""
    kind = "dog"
    actions = ["stand", "sit", "hello"]

    def __init__(self, kind="dog"):
        self.kind = kind
        if kind == "humanoid":
            self.actions = ["stand", "sit", "wave", "shake_hand"]
            self.max_vx, self.max_vy, self.max_yaw = 0.4, 0.2, 0.6
        self.v = (0.0, 0.0, 0.0)
        self.last_action = None

    def connect(self):
        print(f"[mock:{self.kind}] connected")

    def move(self, vx, vy, yaw):
        self.v = (vx, vy, yaw)
        print(f"[mock] move vx={vx:+.2f} vy={vy:+.2f} yaw={yaw:+.2f}")

    def stop(self):
        if self.v != (0.0, 0.0, 0.0):
            print("[mock] STOP")
        self.v = (0.0, 0.0, 0.0)

    def action(self, name):
        self.last_action = name
        print(f"[mock] action {name}")

    def state(self):
        return {"moving": self.v != (0.0, 0.0, 0.0), "last_action": self.last_action, "battery": 87}


# ------------------------------------------------------------------
class Go2Adapter(RobotAdapter):
    """Unitree Go2 (потрібна EDU або доступ до SDK). pip install unitree_sdk2py"""
    kind = "dog"
    actions = ["stand", "sit", "lie_down", "recovery", "hello", "stretch", "balance"]
    max_vx, max_vy, max_yaw = 0.8, 0.4, 1.0

    def __init__(self, iface: str):
        self.iface = iface
        self.client = None

    def connect(self):
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.go2.sport.sport_client import SportClient

        ChannelFactoryInitialize(0, self.iface)
        self.client = SportClient()
        self.client.SetTimeout(5.0)
        self.client.Init()

    def move(self, vx, vy, yaw):
        self.client.Move(vx, vy, yaw)

    def stop(self):
        self.client.StopMove()

    def action(self, name):
        c = self.client
        table = {
            "stand": c.StandUp,
            "sit": c.Sit,
            "lie_down": c.StandDown,
            "recovery": c.RecoveryStand,
            "hello": c.Hello,
            "stretch": c.Stretch,
            "balance": c.BalanceStand,
        }
        if name in table:
            table[name]()


# ------------------------------------------------------------------
class HumanoidAdapter(RobotAdapter):
    """
    Гуманоїди Unitree (G1 / R1). Використовує LocoClient з unitree_sdk2py.
    Для R1 назви методів ще не перевірені — якщо щось падає, дивіться
    приклади з SDK для вашої моделі і правте таблицю в action().
    """
    kind = "humanoid"
    actions = ["stand", "sit", "wave", "shake_hand", "damp"]
    max_vx, max_vy, max_yaw = 0.4, 0.2, 0.6  # гуманоїд — обережніше

    def __init__(self, iface: str):
        self.iface = iface
        self.client = None

    def connect(self):
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient

        ChannelFactoryInitialize(0, self.iface)
        self.client = LocoClient()
        self.client.SetTimeout(5.0)
        self.client.Init()

    def move(self, vx, vy, yaw):
        self.client.Move(vx, vy, yaw)

    def stop(self):
        self.client.StopMove()

    def action(self, name):
        c = self.client
        table = {
            "stand": getattr(c, "StandUp", None),
            "sit": getattr(c, "Sit", None),
            "wave": getattr(c, "WaveHand", None),
            "shake_hand": getattr(c, "ShakeHand", None),
            "damp": getattr(c, "Damp", None),
        }
        fn = table.get(name)
        if fn:
            fn()


def make_adapter(kind: str, iface: str) -> RobotAdapter:
    if kind == "go2":
        return Go2Adapter(iface)
    if kind in ("g1", "r1", "humanoid"):
        return HumanoidAdapter(iface)
    if kind == "mock-dog":
        return MockAdapter("dog")
    if kind == "mock-humanoid":
        return MockAdapter("humanoid")
    raise ValueError(f"unknown robot kind: {kind}")
