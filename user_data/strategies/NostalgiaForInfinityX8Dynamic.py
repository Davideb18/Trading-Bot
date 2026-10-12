from NostalgiaForInfinityX8 import NostalgiaForInfinityX8
import pandas as pd
from datetime import datetime
from typing import Optional

class NostalgiaForInfinityX8Dynamic(NostalgiaForInfinityX8):
    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> bool:
        max_entry_date = self.config.get("max_entry_date")
        if max_entry_date:
            cutoff = pd.to_datetime(max_entry_date, utc=True)
            if current_time >= cutoff:
                return False
        return super().confirm_trade_entry(
            pair=pair,
            order_type=order_type,
            amount=amount,
            rate=rate,
            time_in_force=time_in_force,
            current_time=current_time,
            entry_tag=entry_tag,
            side=side,
            **kwargs,
        )
