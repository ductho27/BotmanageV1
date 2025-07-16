import sys
import time
import threading
from datetime import datetime, timedelta
import pytz

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit,
    QComboBox, QMessageBox, QGridLayout, QTableWidget,
    QTableWidgetItem, QAbstractItemView, QCheckBox, QGroupBox,
    QHeaderView
)
from PyQt5.QtCore import pyqtSignal, QObject, Qt, QTimer
from PyQt5.QtGui import QFont

import MetaTrader5 as mt5


# --- Logger Class ---
class Logger(QObject):
    """
    Lớp này dùng để gửi tin nhắn log từ các luồng khác nhau
    về vùng hiển thị log trên giao diện chính (MainWindow).
    """
    log_signal = pyqtSignal(str)

    def log(self, message):
        """Gửi tin nhắn qua signal để hiển thị trên GUI."""
        self.log_signal.emit(message)


# --- BreakevenProtectorSignals Class ---
class BreakevenProtectorSignals(QObject):
    """
    Lớp này chứa các tín hiệu (signals) mà BreakevenProtector thread
    sẽ phát ra để cập nhật thông tin lên GUI của MainWindow.
    """
    position_update_signal = pyqtSignal(list) # Dùng để cập nhật bảng lệnh mở
    # Signal mới để cập nhật bảng lệnh kích hoạt đang theo dõi
    trigger_monitor_update_signal = pyqtSignal(list)


# --- BreakevenProtector Thread ---
class BreakevenProtector(threading.Thread):
    """
    Luồng (thread) chạy nền để giám sát các lệnh mở,
    tính toán P/L, áp dụng bảo vệ Breakeven và giám sát giới hạn lỗ,
    và xử lý lệnh đặc biệt (Order Trigger).
    """
    def __init__(self, logger, initial_params, constants): ## NEW: Thêm constants
        super().__init__()
        self.logger = logger
        self.running = True      # Cờ điều khiển vòng lặp chính của thread
        self.connected = False   # Cờ trạng thái kết nối MT5
        self.params = initial_params.copy() # Tham số cài đặt bot, bản sao riêng cho thread
        self.breakeven_on = False  # Cờ bật/tắt chức năng breakeven
        self.signals = BreakevenProtectorSignals() # Instance của lớp signals
        self.constants = constants ## NEW: Lưu các hằng số

        # Set để lưu các symbol đã cảnh báo về stops_level/freeze_level (thiếu)
        self.warned_symbols_for_stops_level = set()
        # Set mới để lưu các symbol đã được thông báo là hỗ trợ (đủ)
        self.informed_symbols_with_full_support = set()

        # Set để lưu các ticket đã gặp lỗi dịch SL và đã được báo cáo
        self.reported_sl_modify_errors = set()

        # Dictionary để lưu thông tin 'Giá P' của các lệnh chờ đã được tạo bởi trigger
        # Key: order_ticket, Value: trigger_price_P
        self.triggered_orders_P_price = {}

        # --- NEW: Quản lý nhiều lệnh kích hoạt ---
        # Danh sách các dict, mỗi dict đại diện cho một lệnh kích hoạt
        self.active_triggers = []
        # Set để lưu các ID của trigger đã được kích hoạt (để tránh kích hoạt lại)
        self.activated_trigger_ids = set()

        # Biến đếm để tạo unique ID cho mỗi trigger
        self._next_trigger_id = 1


    def stop(self):
        """Dừng luồng một cách an toàn."""
        self.running = False

    def set_breakeven_on(self, status):
        """Thiết lập trạng thái bật/tắt của Breakeven Protector."""
        self.breakeven_on = status

    def update_global_params(self, new_params):
        """Cập nhật các tham số cấu hình chung (ví dụ: update_interval) từ GUI."""
        self.params.update(new_params)

    # --- NEW: Các phương thức để quản lý lệnh kích hoạt ---
    def add_trigger(self, trigger_config):
        """Thêm một lệnh kích hoạt mới vào danh sách."""
        # Gán một ID duy nhất cho trigger này
        trigger_config['id'] = self._next_trigger_id
        self._next_trigger_id += 1

        # Đảm bảo symbol là chữ hoa và loại bỏ khoảng trắng
        trigger_config['symbol'] = trigger_config['symbol'].strip()

        # Thêm biến để lưu giá trước đó, cần cho logic kích hoạt
        trigger_config['previous_price_for_trigger'] = None

        # Kiểm tra xem có trigger nào có cùng symbol và giá P đã tồn tại không
        for existing_trigger in self.active_triggers:
            if existing_trigger['symbol'] == trigger_config['symbol'] and \
               abs(existing_trigger['price_P'] - trigger_config['price_P']) < 0.000001: # So sánh số thực
                self.logger.log(f"Cảnh báo: Lệnh kích hoạt cho {trigger_config['symbol']} @ {trigger_config['price_P']} đã tồn tại. Không thêm lại.")
                return

        self.active_triggers.append(trigger_config)
        self.logger.log(f"Đã thêm lệnh kích hoạt mới: ID {trigger_config['id']} cho {trigger_config['symbol']} @ {trigger_config['price_P']}")
        # Nếu đã có cùng ID trong activated_trigger_ids, xóa nó đi để cho phép kích hoạt lại
        if trigger_config['id'] in self.activated_trigger_ids:
            self.activated_trigger_ids.remove(trigger_config['id'])
            self.logger.log(f"Reset trạng thái kích hoạt cho trigger ID {trigger_config['id']}.")

    def remove_trigger(self, trigger_id):
        """Xóa một lệnh kích hoạt khỏi danh sách theo ID."""
        original_len = len(self.active_triggers)
        self.active_triggers = [t for t in self.active_triggers if t['id'] != trigger_id]
        if len(self.active_triggers) < original_len:
            self.logger.log(f"Đã xóa lệnh kích hoạt ID: {trigger_id}.")
            # Xóa khỏi set đã kích hoạt nếu có
            if trigger_id in self.activated_trigger_ids:
                self.activated_trigger_ids.remove(trigger_id)
        else:
            self.logger.log(f"Không tìm thấy lệnh kích hoạt ID: {trigger_id} để xóa.")

    def clear_all_triggers(self):
        """Xóa tất cả các lệnh kích hoạt khỏi danh sách."""
        num_cleared = len(self.active_triggers)
        self.active_triggers.clear()
        self.activated_trigger_ids.clear() # Đảm bảo reset trạng thái kích hoạt
        self._next_trigger_id = 1 # Reset ID counter
        self.logger.log(f"Đã xóa tất cả {num_cleared} lệnh kích hoạt.")

    def _perform_end_of_day_cleanup(self):
        """Đóng tất cả lệnh mở, hủy lệnh chờ và xóa trigger khi sang ngày mới UTC."""
        self.logger.log("--- BẮT ĐẦU DỌN DẸP CUỐI NGÀY (UTC) ---")

        # 1. Đóng tất cả các lệnh đang mở (positions)
        self.logger.log("Đang đóng tất cả các lệnh đang mở...")
        positions = mt5.positions_get()
        if not positions:
            self.logger.log("Không có lệnh nào đang mở để đóng.")
        else:
            closed_count = 0
            failed_count = 0
            for pos in positions:
                tick = mt5.symbol_info_tick(pos.symbol)
                if not tick:
                    self.logger.log(f"Lỗi: Không thể lấy tick data cho '{pos.symbol}' để đóng lệnh {pos.ticket}.")
                    failed_count += 1
                    continue

                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "position": pos.ticket,
                    "symbol": pos.symbol,
                    "volume": pos.volume,
                    "deviation": 20,
                    "magic": pos.magic,
                    "comment": "End of Day Cleanup (UTC)",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }

                if pos.type == mt5.ORDER_TYPE_BUY:
                    request["type"] = mt5.ORDER_TYPE_SELL
                    request["price"] = tick.bid
                else: # SELL
                    request["type"] = mt5.ORDER_TYPE_BUY
                    request["price"] = tick.ask

                result = mt5.order_send(request)
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    self.logger.log(f"  -> Đã đóng lệnh {pos.ticket} ({pos.symbol}) thành công.")
                    closed_count += 1
                else:
                    self.logger.log(f"  -> LỖI đóng lệnh {pos.ticket}: {result.retcode if result else 'None'} ({mt5.last_error()})")
                    failed_count += 1
            self.logger.log(f"Hoàn tất đóng lệnh: {closed_count} thành công, {failed_count} thất bại.")

        # 2. Hủy tất cả các lệnh chờ (pending orders)
        self.logger.log("Đang hủy tất cả các lệnh chờ...")
        orders = mt5.orders_get()
        if not orders:
            self.logger.log("Không có lệnh chờ nào để hủy.")
        else:
            cancelled_count = 0
            failed_count = 0
            for order in orders:
                request = {
                    "action": mt5.TRADE_ACTION_REMOVE,
                    "order": order.ticket,
                    "comment": "End of Day Cleanup (UTC)"
                }
                result = mt5.order_send(request)
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    self.logger.log(f"  -> Đã hủy lệnh chờ {order.ticket} ({order.symbol}) thành công.")
                    cancelled_count += 1
                else:
                    self.logger.log(f"  -> LỖI hủy lệnh chờ {order.ticket}: {result.retcode if result else 'None'} ({mt5.last_error()})")
                    failed_count += 1
            self.logger.log(f"Hoàn tất hủy lệnh chờ: {cancelled_count} thành công, {failed_count} thất bại.")

        # 3. Xóa tất cả các lệnh kích hoạt đang theo dõi
        self.logger.log("Đang xóa tất cả các lệnh kích hoạt đang theo dõi...")
        self.clear_all_triggers()
        self.logger.log("--- KẾT THÚC DỌN DẸP CUỐI NGÀY (UTC) ---")

    def run(self):
        """Phương thức chính của luồng, chứa logic hoạt động của bot."""
        self.logger.log("Đang chờ kết nối MT5...")
        # Chờ cho đến khi MT5 được kết nối
        while self.running and not self.connected:
            time.sleep(1)
        if not self.running: # Kiểm tra lại nếu luồng bị dừng trong lúc chờ kết nối
            return
        self.logger.log("MT5 đã kết nối! Bắt đầu giám sát các lệnh.")

        previous_loss = None   # Biến để theo dõi tổng lỗ ngày hôm trước
        current_utc_day = None # Biến để theo dõi ngày UTC

        while self.running:
            # Lấy các tham số cấu hình chung mới nhất từ GUI (ví dụ: update_interval)
            update_interval = self.params.get('update_interval', 5.0)
            max_loss_per_day = self.params.get('max_loss_per_day', -100.0)
            break_even_pips = self.params.get('break_even_pips', 3.0)
            break_even_offset = self.params.get('break_even_offset', 0.5)
            close_all_at_day_end = self.params.get('close_all_at_day_end', False)

            if not self.connected: # Nếu bị ngắt kết nối trong lúc chạy
                time.sleep(1)
                continue

            # --- Logic dọn dẹp cuối ngày (UTC) ---
            now_utc = datetime.now(pytz.utc)
            today_utc_date = now_utc.date()

            # Khởi tạo ngày UTC lần đầu tiên
            if current_utc_day is None:
                current_utc_day = today_utc_date
                self.logger.log(f"Khởi tạo ngày UTC: {current_utc_day}. Chức năng dọn dẹp cuối ngày sẽ bắt đầu từ ngày mai.")

            # Phát hiện khi ngày UTC thay đổi
            elif today_utc_date > current_utc_day:
                self.logger.log(f"Phát hiện ngày UTC mới: {today_utc_date}. Ngày cũ: {current_utc_day}.")
                current_utc_day = today_utc_date # Cập nhật ngày mới

                # Nếu chức năng được bật, thực hiện dọn dẹp
                if close_all_at_day_end:
                    self._perform_end_of_day_cleanup()
                else:
                    self.logger.log("Chức năng dọn dẹp cuối ngày đang tắt, bỏ qua.")

            # Lấy tất cả các vị thế đang mở
            positions = mt5.positions_get()

            # Khởi tạo lại dictionary cho mỗi vòng lặp
            symbol_ticks = {}
            symbol_infos = {}
            valid_positions_for_processing = [] # Danh sách các vị thế có đủ thông tin để xử lý

            # Lấy danh sách ticket của các lệnh đang mở hiện tại
            current_open_position_tickets = {pos.ticket for pos in positions}

            # Xóa các lỗi đã báo cáo cho các lệnh đã đóng hoặc không còn tồn tại
            tickets_to_remove = []
            for ticket in self.reported_sl_modify_errors:
                if ticket not in current_open_position_tickets:
                    tickets_to_remove.append(ticket)
            for ticket in tickets_to_remove:
                self.reported_sl_modify_errors.remove(ticket)

            # --- Lấy thông tin symbol và tick cho tất cả các symbol đang mở + trigger symbols ---
            symbols_to_fetch = {pos.symbol for pos in positions}
            for trigger in self.active_triggers:
                symbols_to_fetch.add(trigger['symbol'])

            for sym in symbols_to_fetch:
                s_info = mt5.symbol_info(sym)
                s_tick = mt5.symbol_info_tick(sym)

                if s_info is None:
                    self.logger.log(f"Cảnh báo: Không thể lấy thông tin symbol cho '{sym}'. Bỏ qua symbol này.")
                    continue

                if not s_info.visible:
                    if not mt5.symbol_select(sym, True):
                        self.logger.log(f"Cảnh báo: Không thể làm hiển thị symbol '{sym}'. Bỏ qua symbol này.")
                        continue
                    s_info = mt5.symbol_info(sym)
                    if s_info is None:
                        self.logger.log(f"Cảnh báo: Lấy lại thông tin symbol '{sym}' sau khi làm hiển thị thất bại. Bỏ qua symbol này.")
                        continue

                if s_tick is None:
                    self.logger.log(f"Cảnh báo: Không thể lấy tick data cho '{sym}'. Bỏ qua symbol này.")
                    continue

                # --- Kiểm tra thuộc tính stops_level/freeze_level và ghi cảnh báo/thông báo một lần ---
                if not hasattr(s_info, 'stops_level') or not hasattr(s_info, 'freeze_level'):
                    if sym not in self.warned_symbols_for_stops_level:
                        self.logger.log(f"Cảnh báo: SymbolInfo cho '{sym}' thiếu thuộc tính 'stops_level' hoặc 'freeze_level'. Breakeven Protector sẽ coi các giới hạn này là 0.")
                        self.warned_symbols_for_stops_level.add(sym)
                    if sym in self.informed_symbols_with_full_support:
                        self.informed_symbols_with_full_support.remove(sym)
                else:
                    if sym in self.warned_symbols_for_stops_level:
                        self.warned_symbols_for_stops_level.remove(sym)
                        self.logger.log(f"Thông báo: Symbol '{sym}' hiện đã có đủ thuộc tính 'stops_level' và 'freeze_level'.")
                    if sym not in self.informed_symbols_with_full_support:
                        if s_info.stops_level > 0 or s_info.freeze_level > 0:
                            self.logger.log(f"Thông báo: Symbol '{sym}' hỗ trợ đầy đủ 'stops_level' ({s_info.stops_level} points) và 'freeze_level' ({s_info.freeze_level} points). Breakeven Protector có thể hoạt động.")
                        else:
                            self.logger.log(f"Thông báo: Symbol '{sym}' có 'stops_level' ({s_info.stops_level} points) và 'freeze_level' ({s_info.freeze_level} points) nhưng giá trị bằng 0. Breakeven Protector có thể không cần tuân thủ khoảng cách tối thiểu.")
                        self.informed_symbols_with_full_support.add(sym)

                symbol_infos[sym] = s_info
                symbol_ticks[sym] = s_tick

            # Lọc các vị thế hợp lệ sau khi đã lấy được thông tin symbol
            for pos in positions:
                if pos.symbol in symbol_infos and pos.symbol in symbol_ticks:
                    valid_positions_for_processing.append(pos)


            # --- Tính toán P/L Pips và cập nhật bảng lệnh mở trên GUI ---
            positions_data = []
            for pos in valid_positions_for_processing:
                symbol_info = symbol_infos[pos.symbol]
                tick = symbol_ticks[pos.symbol]

                profit_pips = 0.0
                current_price = 0.0

                pip_step = 0.0
                if symbol_info.digits == 5: pip_step = 0.0001
                elif symbol_info.digits == 3: pip_step = 0.001
                elif symbol_info.digits == 2: pip_step = 0.1
                else: pip_step = 10 * symbol_info.point

                if pip_step > 0:
                    current_price = tick.ask if pos.type == mt5.ORDER_TYPE_BUY else tick.bid
                    profit_pips = (current_price - pos.price_open) / pip_step
                    if pos.type == mt5.ORDER_TYPE_SELL:
                        profit_pips = -profit_pips

                positions_data.append({
                    "ticket": pos.ticket,
                    "symbol": pos.symbol,
                    "type": "Buy" if pos.type == mt5.ORDER_TYPE_BUY else "Sell",
                    "volume": pos.volume,
                    "price_open": pos.price_open,
                    "current_price": current_price,
                    "sl": pos.sl,
                    "tp": pos.tp,
                    "profit_usd": pos.profit,
                    "profit_pips": profit_pips
                })

            self.signals.position_update_signal.emit(positions_data) # Gửi dữ liệu về GUI

            # --- Logic Breakeven Protector ---
            if self.breakeven_on:
                for pos in valid_positions_for_processing:
                    symbol_info = symbol_infos[pos.symbol]
                    tick = symbol_ticks[pos.symbol]

                    pip_step = 0.0
                    if symbol_info.digits == 5: pip_step = 0.0001
                    elif symbol_info.digits == 3: pip_step = 0.001
                    elif symbol_info.digits == 2: pip_step = 0.1
                    else: pip_step = 10 * symbol_info.point

                    if pip_step == 0.0:
                        self.logger.log(f"Không thể xác định giá trị pip cho symbol {pos.symbol} để tính Breakeven. Bỏ qua.")
                        continue

                    curr_price = tick.ask if pos.type == mt5.ORDER_TYPE_BUY else tick.bid

                    current_profit_pips = abs((curr_price - pos.price_open) / pip_step)

                    new_sl = pos.price_open + break_even_offset * pip_step if pos.type == mt5.ORDER_TYPE_BUY else pos.price_open - break_even_offset * pip_step

                    stop_level_points = 0.0
                    freeze_level_points = 0.0

                    if hasattr(symbol_info, 'stops_level'):
                        stop_level_points = symbol_info.stops_level * symbol_info.point

                    if hasattr(symbol_info, 'freeze_level'):
                        freeze_level_points = symbol_info.freeze_level * symbol_info.point

                    can_modify = False
                    enough_profit = current_profit_pips >= break_even_pips
                    sl_far_enough = True
                    in_freeze = False

                    if pos.type == mt5.ORDER_TYPE_BUY:
                        # SL mới phải ít nhất bằng giá mở lệnh
                        if new_sl < pos.price_open:
                            new_sl = pos.price_open

                        # Chỉ kiểm tra nếu stops_level > 0
                        if stop_level_points > 0 and new_sl >= curr_price - stop_level_points:
                                sl_far_enough = False

                        # Nếu SL hiện tại của lệnh đã tốt hơn SL mới được tính, không cần dời
                        if pos.sl != 0.0 and pos.sl >= new_sl: sl_far_enough = False

                    elif pos.type == mt5.ORDER_TYPE_SELL:
                        # SL mới phải ít nhất bằng giá mở lệnh
                        if new_sl > pos.price_open:
                            new_sl = pos.price_open

                        if stop_level_points > 0 and new_sl <= curr_price + stop_level_points:
                            sl_far_enough = False

                        if pos.sl != 0.0 and pos.sl <= new_sl: sl_far_enough = False

                    if freeze_level_points > 0 and abs(curr_price - pos.price_open) < freeze_level_points:
                            in_freeze = True

                    # Chỉ được modify nếu đủ profit VÀ SL đủ xa khỏi giá hiện tại/freeze level VÀ SL hiện tại chưa được đặt hoặc tệ hơn SL mới
                    if enough_profit and sl_far_enough and not in_freeze:
                        if (pos.sl == 0.0) or \
                            (pos.type == mt5.ORDER_TYPE_BUY and pos.sl < new_sl) or \
                            (pos.type == mt5.ORDER_TYPE_SELL and pos.sl > new_sl):
                            can_modify = True

                    if can_modify:
                        new_sl = round(new_sl, symbol_info.digits)
                        old_sl = pos.sl if pos.sl != 0.0 else 0.0

                        request = {
                            "action": mt5.TRADE_ACTION_SLTP,
                            "position": pos.ticket,
                            "sl": new_sl,
                            "tp": pos.tp
                        }
                        res = mt5.order_send(request)
                        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                            # Nếu thành công, xóa khỏi set lỗi đã báo cáo
                            if pos.ticket in self.reported_sl_modify_errors:
                                self.reported_sl_modify_errors.remove(pos.ticket)
                            self.logger.log(f"[{pos.symbol}] Đã dời SL về BE cho lệnh {pos.ticket} ({'Buy' if pos.type==0 else 'Sell'}), SL cũ: {old_sl:.{symbol_info.digits}f}, SL mới: {new_sl:.{symbol_info.digits}f}")
                        else:
                            # Chỉ ghi log nếu lỗi này chưa được báo cáo
                            if pos.ticket not in self.reported_sl_modify_errors:
                                self.logger.log(f"[{pos.symbol}] LỖI: Không thể dời SL BE lệnh {pos.ticket}: {res.retcode if res else 'None'} ({mt5.last_error()})")
                                self.reported_sl_modify_errors.add(pos.ticket)

            # --- Logic Order Trigger (Lệnh kích hoạt) ---
            trigger_monitor_data = [] # Dữ liệu để gửi lên bảng theo dõi GUI
            for trigger_config in self.active_triggers:
                trigger_id = trigger_config['id']
                trigger_symbol = trigger_config['symbol']
                trigger_price_P = trigger_config['price_P']
                buy_stop_offset_pips = trigger_config['buy_stop_offset_pips']
                sell_stop_offset_pips = trigger_config['sell_stop_offset_pips']
                triggered_orders_lot_size = trigger_config['triggered_orders_lot_size']
                triggered_orders_tp_pips = trigger_config['triggered_orders_tp_pips']
                triggered_orders_sl_pips = trigger_config['triggered_orders_sl_pips']

                ## FIX 1: Lấy `order_type` từ config để khắc phục lỗi NameError
                order_type = trigger_config.get('order_type', 'Double Stop')

                # Lấy giá trước đó cho trigger này
                previous_price_for_trigger = trigger_config['previous_price_for_trigger']

                current_price_for_trigger_display = 0.0
                status_display = "Đang chờ"

                # Lấy giá hiện tại để cập nhật bảng, ngay cả khi trigger đã kích hoạt
                if trigger_symbol in symbol_ticks:
                    tick = symbol_ticks[trigger_symbol]
                    current_price_for_trigger_display = tick.last if tick.last != 0 else (tick.bid + tick.ask) / 2
                else:
                    status_display = "Lỗi symbol"

                if trigger_id in self.activated_trigger_ids:
                    status_display = "Đã kích hoạt"
                
                # Chỉ xử lý nếu trigger chưa được kích hoạt và có đủ thông tin symbol
                if trigger_id not in self.activated_trigger_ids:
                    if trigger_symbol in symbol_infos and trigger_symbol in symbol_ticks:
                        symbol_info = symbol_infos[trigger_symbol]
                        tick = symbol_ticks[trigger_symbol]

                        pip_step = 0.0
                        if symbol_info.digits == 5: pip_step = 0.0001
                        elif symbol_info.digits == 3: pip_step = 0.001
                        elif symbol_info.digits == 2: pip_step = 0.1
                        else: pip_step = 10 * symbol_info.point

                        if pip_step == 0.0:
                            status_display = "Lỗi pip_step"
                        else:
                            current_price_for_trigger = current_price_for_trigger_display

                            # Cập nhật previous_price_for_trigger lần đầu
                            if previous_price_for_trigger is None:
                                trigger_config['previous_price_for_trigger'] = current_price_for_trigger
                                previous_price_for_trigger = current_price_for_trigger
                                self.logger.log(f"[{trigger_symbol}] Khởi tạo previous_price_for_trigger cho ID {trigger_id}: {previous_price_for_trigger:.{symbol_info.digits}f}")
                                status_display = "Đang chờ (lần đầu)"
                            else:
                                # --- Logic KÍCH HOẠT MỚI: Phát hiện giao cắt qua điểm P ---
                                price_P_crossed = False
                                if previous_price_for_trigger < trigger_price_P and current_price_for_trigger >= trigger_price_P:
                                    price_P_crossed = True
                                    self.logger.log(f"[{trigger_symbol}] Lệnh kích hoạt ID {trigger_id}: Giá đã TĂNG qua điểm P: {trigger_price_P:.{symbol_info.digits}f}")
                                elif previous_price_for_trigger > trigger_price_P and current_price_for_trigger <= trigger_price_P:
                                    price_P_crossed = True
                                    self.logger.log(f"[{trigger_symbol}] Lệnh kích hoạt ID {trigger_id}: Giá đã GIẢM qua điểm P: {trigger_price_P:.{symbol_info.digits}f}")

                                ## FIX 2: Cấu trúc lại toàn bộ logic đặt lệnh và kích hoạt
                                if price_P_crossed:
                                    self.logger.log(f"[{trigger_symbol}] Phát hiện giao cắt. Loại lệnh: {order_type}.")
                                    
                                    placed_buy_successfully = False
                                    placed_sell_successfully = False

                                    # --- Đặt lệnh Buy Stop (nếu cần) ---
                                    if order_type in ["Double Stop", "Buy Stop"]:
                                        buy_stop_price = trigger_price_P + buy_stop_offset_pips * pip_step
                                        buy_stop_tp = buy_stop_price + triggered_orders_tp_pips * pip_step if triggered_orders_tp_pips > 0 else 0.0
                                        buy_stop_sl = buy_stop_price - triggered_orders_sl_pips * pip_step if triggered_orders_sl_pips > 0 else 0.0
                                        
                                        buy_stop_price = round(buy_stop_price, symbol_info.digits)
                                        buy_stop_tp = round(buy_stop_tp, symbol_info.digits)
                                        buy_stop_sl = round(buy_stop_sl, symbol_info.digits)

                                        req_buy_stop = {
                                            "action": mt5.TRADE_ACTION_PENDING, "symbol": trigger_symbol,
                                            "volume": triggered_orders_lot_size, "type": mt5.ORDER_TYPE_BUY_STOP,
                                            "price": buy_stop_price, "sl": buy_stop_sl, "tp": buy_stop_tp,
                                            "deviation": 0, "magic": self.constants['TRIGGER_BUY_MAGIC'],
                                            "comment": f"Buy Stop from Trigger {trigger_id}",
                                            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
                                        }
                                        res_buy_stop = mt5.order_send(req_buy_stop)
                                        if res_buy_stop and res_buy_stop.retcode == mt5.TRADE_RETCODE_DONE:
                                                            self.logger.log(f"[{trigger_symbol}] Đã đặt Buy Stop thành công! Ticket: {res_buy_stop.order}, Giá: {buy_stop_price:.{symbol_info.digits}f}")
                                                            self.triggered_orders_P_price[res_buy_stop.order] = trigger_price_P
                                                            placed_buy_successfully = True
                                        else:
                                            self.logger.log(f"[{trigger_symbol}] LỖI: Đặt Buy Stop thất bại: {res_buy_stop.retcode if res_buy_stop else 'None'} ({mt5.last_error()})")

                                    # --- Đặt lệnh Sell Stop (nếu cần) ---
                                    if order_type in ["Double Stop", "Sell Stop"]:
                                        sell_stop_price = trigger_price_P - sell_stop_offset_pips * pip_step
                                        sell_stop_tp = sell_stop_price - triggered_orders_tp_pips * pip_step if triggered_orders_tp_pips > 0 else 0.0
                                        sell_stop_sl = sell_stop_price + triggered_orders_sl_pips * pip_step if triggered_orders_sl_pips > 0 else 0.0
                                        
                                        sell_stop_price = round(sell_stop_price, symbol_info.digits)
                                        sell_stop_tp = round(sell_stop_tp, symbol_info.digits)
                                        sell_stop_sl = round(sell_stop_sl, symbol_info.digits)

                                        req_sell_stop = {
                                            "action": mt5.TRADE_ACTION_PENDING, "symbol": trigger_symbol,
                                            "volume": triggered_orders_lot_size, "type": mt5.ORDER_TYPE_SELL_STOP,
                                            "price": sell_stop_price, "sl": sell_stop_sl, "tp": sell_stop_tp,
                                            "deviation": 0, "magic": self.constants['TRIGGER_SELL_MAGIC'],
                                            "comment": f"Sell Stop from Trigger {trigger_id}",
                                            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
                                        }
                                        res_sell_stop = mt5.order_send(req_sell_stop)
                                        if res_sell_stop and res_sell_stop.retcode == mt5.TRADE_RETCODE_DONE:
                                            self.logger.log(f"[{trigger_symbol}] Đã đặt Sell Stop thành công! Ticket: {res_sell_stop.order}, Giá: {sell_stop_price:.{symbol_info.digits}f}")
                                            self.triggered_orders_P_price[res_sell_stop.order] = trigger_price_P
                                            placed_sell_successfully = True
                                        else:
                                            self.logger.log(f"[{trigger_symbol}] LỖI: Đặt Sell Stop thất bại: {res_sell_stop.retcode if res_sell_stop else 'None'} ({mt5.last_error()})")
                                    
                                    # --- Đánh dấu trigger đã kích hoạt ---
                                    should_activate = False
                                    if order_type == "Double Stop" and (placed_buy_successfully or placed_sell_successfully):
                                        should_activate = True
                                    elif order_type == "Buy Stop" and placed_buy_successfully:
                                        should_activate = True
                                    elif order_type == "Sell Stop" and placed_sell_successfully:
                                        should_activate = True
                                    
                                    if should_activate:
                                        self.activated_trigger_ids.add(trigger_id)
                                        status_display = "Đã kích hoạt" # Cập nhật trạng thái ngay lập tức
                                        self.logger.log(f"[{trigger_symbol}] Trigger ID {trigger_id} đã được kích hoạt và sẽ không chạy lại.")
                                    else:
                                        self.logger.log(f"[{trigger_symbol}] Không thể đặt lệnh cho trigger ID {trigger_id}. Sẽ thử lại.")

                            # Cập nhật giá trước đó cho lần lặp tiếp theo
                            trigger_config['previous_price_for_trigger'] = current_price_for_trigger
                    else:
                        if trigger_symbol not in self.warned_symbols_for_stops_level:
                            self.logger.log(f"Cảnh báo: Không thể lấy thông tin hoặc tick data cho symbol '{trigger_symbol}' của trigger ID {trigger_id}.")
                            self.warned_symbols_for_stops_level.add(trigger_symbol)
                        status_display = "Lỗi symbol"

                # Cập nhật dữ liệu để hiển thị trên bảng theo dõi
                trigger_monitor_data.append({
                    'id': trigger_id,
                    'symbol': trigger_symbol,
                    'price_P': trigger_price_P,
                    'current_price': current_price_for_trigger_display,
                    'status': status_display,
                    'order_type': order_type ## FIX 3: Thêm order_type vào dữ liệu gửi lên GUI
                })

            # Sau khi xử lý tất cả các trigger, gửi dữ liệu lên GUI
            # Lọc chỉ những trigger chưa được kích hoạt hoặc đang có lỗi để hiển thị
            self.signals.trigger_monitor_update_signal.emit([
                t for t in trigger_monitor_data if t['status'] not in ["Đã kích hoạt"]
            ])

            # --- Tính toán lãi/lỗ trong ngày ---
            try:
                now = datetime.now(pytz.UTC)
                start = now.replace(hour=0, minute=0, second=0, microsecond=0)

                deals = mt5.history_deals_get(start.timestamp(), now.timestamp())

                total_profit_today = sum(d.profit for d in deals if d.entry == mt5.DEAL_ENTRY_OUT)

                if total_profit_today <= max_loss_per_day and total_profit_today < 0:
                    self.logger.log(f"CẢNH BÁO: Đã vượt giới hạn lỗ {max_loss_per_day} USD, bạn cần kiểm soát rủi ro!")
            except Exception as e:
                self.logger.log(f"Lỗi khi tính toán tổng lãi/lỗ hôm nay: {e}")

            time.sleep(update_interval)


# --- MainWindow Class (GUI) ---
class MainWindow(QWidget):
    """
    Lớp giao diện người dùng chính của ứng dụng.
    """
    ## NEW: Định nghĩa hằng số cho Magic Numbers để code dễ đọc hơn
    MANUAL_ORDER_MAGIC = 123456
    TRIGGER_BUY_MAGIC = 123457
    TRIGGER_SELL_MAGIC = 123458

    def __init__(self):
        super().__init__()
        self.setWindowTitle("MT5 Breakeven Protector & Order Trigger")
        self.resize(1200, 800)

        self.logger = Logger()
        self.logger.log_signal.connect(self.append_log) # Kết nối signal log với UI

        self.mt5_connected = False
        self.breakeven_on = False
        
        ## NEW: Tạo một dict chứa các hằng số để truyền vào thread
        self.constants = {
            'MANUAL_ORDER_MAGIC': self.MANUAL_ORDER_MAGIC,
            'TRIGGER_BUY_MAGIC': self.TRIGGER_BUY_MAGIC,
            'TRIGGER_SELL_MAGIC': self.TRIGGER_SELL_MAGIC,
        }

        # Tham số cài đặt mặc định (chỉ những cái chung cho toàn bộ app, không phải của từng trigger)
        self.default_global_params = {
            "break_even_pips": 3.0,
            "break_even_offset": 0.5,
            "max_loss_per_day": -100.0,
            "update_interval": 5.0,
            "close_all_at_day_end": False, # Thêm tham số mới
        }
        self._global_params = self.default_global_params.copy() # Tạo bản sao để sửa đổi

        # Tham số mặc định cho một lệnh trigger mới
        self.default_trigger_params = {
            "buy_stop_offset_pips": 5.0,
            "sell_stop_offset_pips": 5.0,
            "triggered_orders_lot_size": 0.01,
            "triggered_orders_tp_pips": 30.0,
            "triggered_orders_sl_pips": 30.0
        }


        # Khởi tạo và chạy luồng bảo vệ Breakeven
        self.protector_thread = BreakevenProtector(self.logger, self._global_params.copy(), self.constants)
        # Kết nối signal từ luồng protector_thread đến phương thức cập nhật UI
        self.protector_thread.signals.position_update_signal.connect(self.update_open_positions_table)
        self.protector_thread.signals.trigger_monitor_update_signal.connect(self.update_trigger_monitor_table)
        self.protector_thread.start()

        # QTimer để refresh lệnh chờ (chạy trên luồng chính GUI)
        self.pending_orders_timer = QTimer(self)
        self.pending_orders_timer.timeout.connect(self.refresh_pending_orders)
        self.pending_orders_timer.start(5000) # Cập nhật mỗi 5 giây

        # QTimer cho đồng hồ đếm ngược
        self.eod_countdown_timer = QTimer(self)
        self.eod_countdown_timer.timeout.connect(self.update_eod_countdown)
        # Timer sẽ được start/stop trong on_eod_cleanup_toggle

        self.create_ui()
        self.apply_default_settings() # Áp dụng cài đặt mặc định khi khởi tạo

    def create_ui(self):
        """Tạo và bố trí các thành phần giao diện người dùng."""
        layout = QVBoxLayout()
        main_h_layout = QHBoxLayout() # Layout chính chia đôi
        left_panel = QVBoxLayout()    # Panel bên trái cho cài đặt và lệnh mới
        right_panel = QVBoxLayout()   # Panel bên phải cho bảng lệnh và log

        # --- Cài đặt kết nối và Breakeven ---
        settings_group = QGroupBox("Cài đặt chung")
        grid_settings = QGridLayout()
        settings_group.setLayout(grid_settings)

        grid_settings.addWidget(QLabel("Tài khoản MT5:"), 0, 0)
        self.account_input = QLineEdit()
        grid_settings.addWidget(self.account_input, 0, 1)

        grid_settings.addWidget(QLabel("Mật khẩu:"), 1, 0)
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)
        grid_settings.addWidget(self.password_input, 1, 1)

        grid_settings.addWidget(QLabel("Server:"), 2, 0)
        self.server_input = QLineEdit()
        grid_settings.addWidget(self.server_input, 2, 1)

        grid_settings.addWidget(QLabel("Thời gian cập nhật (giây):"), 3, 0)
        self.update_interval_input = QLineEdit()
        grid_settings.addWidget(self.update_interval_input, 3, 1)

        self.connect_btn = QPushButton("Kết nối MT5")
        self.connect_btn.clicked.connect(self.connect_mt5)
        grid_settings.addWidget(self.connect_btn, 4, 0, 1, 2)

        self.breakeven_checkbox = QCheckBox("Bật bảo vệ Breakeven cho tất cả lệnh mở")
        self.breakeven_checkbox.stateChanged.connect(self.on_breakeven_toggle)
        self.breakeven_checkbox.setEnabled(False) # Ban đầu disabled
        grid_settings.addWidget(self.breakeven_checkbox, 5, 0, 1, 2)

        grid_settings.addWidget(QLabel("Break-even pips:"), 6, 0)
        self.be_pips_input = QLineEdit()
        grid_settings.addWidget(self.be_pips_input, 6, 1)

        grid_settings.addWidget(QLabel("Break-even offset (pips):"), 7, 0)
        self.be_offset_input = QLineEdit()
        grid_settings.addWidget(self.be_offset_input, 7, 1)

        grid_settings.addWidget(QLabel("Giới hạn lỗ tối đa trong ngày (USD):"), 8, 0)
        self.max_loss_input = QLineEdit()
        grid_settings.addWidget(self.max_loss_input, 8, 1)

        # Checkbox và đồng hồ đếm ngược dọn dẹp cuối ngày
        eod_layout = QHBoxLayout()
        self.eod_cleanup_checkbox = QCheckBox("Hủy tất cả lệnh vào cuối ngày (UTC)")
        self.eod_cleanup_checkbox.stateChanged.connect(self.on_eod_cleanup_toggle)
        self.eod_cleanup_checkbox.setEnabled(False) # Ban đầu disabled
        eod_layout.addWidget(self.eod_cleanup_checkbox)

        self.eod_countdown_label = QLabel("") # Label cho đồng hồ
        self.eod_countdown_label.setStyleSheet("color: #007BFF; font-weight: bold;")
        self.eod_countdown_label.setVisible(False) # Ban đầu ẩn
        eod_layout.addWidget(self.eod_countdown_label)
        eod_layout.addStretch() # Đẩy các widget về bên trái

        grid_settings.addLayout(eod_layout, 9, 0, 1, 2)

        left_panel.addWidget(settings_group)

        # --- Cài đặt Lệnh Đặc Biệt (Order Trigger) ---
        order_trigger_group = QGroupBox("Cài đặt Thêm Lệnh Kích Hoạt")
        grid_trigger = QGridLayout()
        order_trigger_group.setLayout(grid_trigger)

        grid_trigger.addWidget(QLabel("Cặp tiền (Symbol):"), 0, 0)
        self.new_trigger_symbol_input = QLineEdit()
        self.new_trigger_symbol_input.setPlaceholderText("VD: EURUSD, XAUUSD")
        grid_trigger.addWidget(self.new_trigger_symbol_input, 0, 1)

        grid_trigger.addWidget(QLabel("Giá kích hoạt P:"), 1, 0)
        self.new_trigger_price_P_input = QLineEdit()
        self.new_trigger_price_P_input.setPlaceholderText("Giá đạt đến để kích hoạt")
        grid_trigger.addWidget(self.new_trigger_price_P_input, 1, 1)

        grid_trigger.addWidget(QLabel("Loại lệnh kích hoạt:"), 2, 0)
        self.trigger_order_type_combo = QComboBox()
        self.trigger_order_type_combo.addItems(["Double Stop", "Buy Stop", "Sell Stop"])
        self.trigger_order_type_combo.currentIndexChanged.connect(self.update_trigger_offset_visibility)
        grid_trigger.addWidget(self.trigger_order_type_combo, 2, 1)

        self.buy_offset_label = QLabel("Buy Stop Offset (pips từ P):")
        grid_trigger.addWidget(self.buy_offset_label, 3, 0)
        self.new_buy_stop_offset_input = QLineEdit()
        grid_trigger.addWidget(self.new_buy_stop_offset_input, 3, 1)

        self.sell_offset_label = QLabel("Sell Stop Offset (pips từ P):")
        grid_trigger.addWidget(self.sell_offset_label, 4, 0)
        self.new_sell_stop_offset_input = QLineEdit()
        grid_trigger.addWidget(self.new_sell_stop_offset_input, 4, 1)

        grid_trigger.addWidget(QLabel("Lot Size:"), 5, 0)
        self.new_triggered_lot_input = QLineEdit()
        grid_trigger.addWidget(self.new_triggered_lot_input, 5, 1)

        grid_trigger.addWidget(QLabel("TP (pips):"), 6, 0)
        self.new_triggered_tp_input = QLineEdit()
        grid_trigger.addWidget(self.new_triggered_tp_input, 6, 1)

        grid_trigger.addWidget(QLabel("SL (pips):"), 7, 0)
        self.new_triggered_sl_input = QLineEdit()
        grid_trigger.addWidget(self.new_triggered_sl_input, 7, 1)

        self.update_trigger_offset_visibility()

        self.add_trigger_btn = QPushButton("Thêm Lệnh Kích Hoạt")
        self.add_trigger_btn.clicked.connect(self.add_new_trigger)
        self.add_trigger_btn.setEnabled(False) # Ban đầu disabled
        grid_trigger.addWidget(self.add_trigger_btn, 8, 0, 1, 2)

        left_panel.addWidget(order_trigger_group)

        # --- Bảng theo dõi lệnh kích hoạt ---
        trigger_monitor_group = QGroupBox("Theo dõi Lệnh Kích Hoạt")
        trigger_monitor_layout = QVBoxLayout()
        trigger_monitor_group.setLayout(trigger_monitor_layout)

        self.trigger_monitor_table = QTableWidget()
        self.trigger_monitor_table.setColumnCount(6)
        self.trigger_monitor_table.setHorizontalHeaderLabels([
            'ID', 'Symbol', 'Giá Kích Hoạt P', 'Giá Hiện Tại', 'Trạng Thái', 'Loại Lệnh'
        ])
        self.trigger_monitor_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.trigger_monitor_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.trigger_monitor_table.setMinimumHeight(130)
        self.trigger_monitor_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        trigger_monitor_layout.addWidget(self.trigger_monitor_table)

        trigger_btn_layout = QHBoxLayout()
        self.remove_selected_trigger_btn = QPushButton("Xóa Lệnh Kích Hoạt Đã Chọn")
        self.remove_selected_trigger_btn.clicked.connect(self.remove_selected_trigger)
        self.remove_selected_trigger_btn.setEnabled(False)
        trigger_btn_layout.addWidget(self.remove_selected_trigger_btn)

        self.clear_all_triggers_btn = QPushButton("Xóa Tất Cả Lệnh Kích Hoạt")
        self.clear_all_triggers_btn.clicked.connect(self.clear_all_triggers)
        self.clear_all_triggers_btn.setEnabled(False)
        trigger_btn_layout.addWidget(self.clear_all_triggers_btn)
        trigger_monitor_layout.addLayout(trigger_btn_layout)
        left_panel.addWidget(trigger_monitor_group)


        # --- Đặt lệnh chờ/lệnh thị trường ---
        order_placement_group = QGroupBox("Đặt lệnh mới (thị trường/chờ)")
        grid_order = QGridLayout()
        order_placement_group.setLayout(grid_order)

        grid_order.addWidget(QLabel("Cặp tiền (Symbol):"), 0, 0)
        self.symbol_input = QLineEdit()
        self.symbol_input.setPlaceholderText("VD: EURUSD, XAUUSD")
        grid_order.addWidget(self.symbol_input, 0, 1)

        grid_order.addWidget(QLabel("Loại lệnh:"), 1, 0)
        self.pending_type_combo = QComboBox()
        self.pending_type_combo.addItems([
            "Buy", "Sell", "Buy Limit", "Sell Limit", "Buy Stop", "Sell Stop"
        ])
        grid_order.addWidget(self.pending_type_combo, 1, 1)

        grid_order.addWidget(QLabel("Giá đặt lệnh (chỉ cho lệnh chờ):"), 2, 0)
        self.pending_price_input = QLineEdit()
        self.pending_price_input.setPlaceholderText("Không cần nhập cho lệnh thị trường")
        grid_order.addWidget(self.pending_price_input, 2, 1)

        grid_order.addWidget(QLabel("Lot size:"), 3, 0)
        self.pending_lot_input = QLineEdit("0.01")
        grid_order.addWidget(self.pending_lot_input, 3, 1)

        grid_order.addWidget(QLabel("TP (pips):"), 4, 0)
        self.tp_pips_input = QLineEdit("50")
        grid_order.addWidget(self.tp_pips_input, 4, 1)

        grid_order.addWidget(QLabel("SL (pips):"), 5, 0)
        self.sl_pips_input = QLineEdit("100")
        grid_order.addWidget(self.sl_pips_input, 5, 1)

        self.place_order_btn = QPushButton("Vào lệnh")
        self.place_order_btn.clicked.connect(self.place_order)
        self.place_order_btn.setEnabled(False) # Ban đầu disabled
        grid_order.addWidget(self.place_order_btn, 6, 0, 1, 2)
        left_panel.addWidget(order_placement_group)

        self.pending_type_combo.currentIndexChanged.connect(self.update_price_input_state)
        self.update_price_input_state() # Khởi tạo trạng thái ban đầu

        main_h_layout.addLayout(left_panel)

        # --- Danh sách lệnh chờ ---
        pending_orders_group = QGroupBox("Danh sách lệnh chờ")
        pending_order_layout = QVBoxLayout()
        pending_orders_group.setLayout(pending_order_layout)

        self.pending_orders_table = QTableWidget()
        self.pending_orders_table.setColumnCount(8)
        self.pending_orders_table.setHorizontalHeaderLabels([
            'Order', 'Symbol', 'Type', 'Price', 'Volume', 'TP', 'SL', 'Giá Kích Hoạt P'
        ])
        self.pending_orders_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.pending_orders_table.setMinimumHeight(130)
        self.pending_orders_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        pending_order_layout.addWidget(self.pending_orders_table)

        pending_btn_layout = QHBoxLayout()
        self.modify_pending_btn = QPushButton("Sửa lệnh chờ")
        self.modify_pending_btn.clicked.connect(self.modify_pending_order)
        self.modify_pending_btn.setEnabled(False)
        pending_btn_layout.addWidget(self.modify_pending_btn)
        self.cancel_pending_btn = QPushButton("Huỷ lệnh chờ")
        self.cancel_pending_btn.clicked.connect(self.cancel_pending_order)
        self.cancel_pending_btn.setEnabled(False)
        pending_btn_layout.addWidget(self.cancel_pending_btn)
        pending_order_layout.addLayout(pending_btn_layout)
        right_panel.addWidget(pending_orders_group)


        # --- Bảng lệnh đang mở ---
        open_positions_group = QGroupBox("Danh sách lệnh đang mở")
        open_positions_layout = QVBoxLayout()
        open_positions_group.setLayout(open_positions_layout)

        self.open_positions_table = QTableWidget()
        self.open_positions_table.setColumnCount(4)
        self.open_positions_table.setHorizontalHeaderLabels([
            'Ticket', 'Symbol', 'P/L (Pips)', 'P/L (USD)'
        ])
        self.open_positions_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.open_positions_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.open_positions_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        open_positions_layout.addWidget(self.open_positions_table)
        right_panel.addWidget(open_positions_group)

        # --- Các nút điều khiển mới ---
        control_buttons_layout = QHBoxLayout()
        self.close_all_positions_btn = QPushButton("Đóng tất cả lệnh đang mở")
        self.close_all_positions_btn.clicked.connect(self.close_all_open_positions)
        self.close_all_positions_btn.setEnabled(False)
        control_buttons_layout.addWidget(self.close_all_positions_btn)

        self.reset_program_btn = QPushButton("Reset Chương Trình")
        self.reset_program_btn.clicked.connect(self.reset_program)
        control_buttons_layout.addWidget(self.reset_program_btn)

        self.exit_app_btn = QPushButton("Thoát Chương Trình")
        self.exit_app_btn.clicked.connect(self.close) # Kết nối với closeEvent
        control_buttons_layout.addWidget(self.exit_app_btn)

        right_panel.addLayout(control_buttons_layout)


        # --- Vùng Log ---
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        right_panel.addWidget(QLabel("Log trạng thái:"))
        right_panel.addWidget(self.log_area)

        main_h_layout.addLayout(right_panel)

        layout.addLayout(main_h_layout)
        self.setLayout(layout)

    def apply_default_settings(self):
        """Áp dụng các giá trị mặc định vào các trường nhập liệu."""
        self.account_input.setText("42040134")
        self.password_input.setText("@Ductho9")
        self.server_input.setText("Exness-MT5Trial4")
        self.update_interval_input.setText(str(self.default_global_params["update_interval"]))
        self.be_pips_input.setText(str(self.default_global_params["break_even_pips"]))
        self.be_offset_input.setText(str(self.default_global_params["break_even_offset"]))
        self.max_loss_input.setText(str(self.default_global_params["max_loss_per_day"]))
        self.symbol_input.setText("EURUSD")
        self.pending_lot_input.setText("0.01")
        self.tp_pips_input.setText("50")
        self.sl_pips_input.setText("100")

        self.new_trigger_symbol_input.setText("")
        self.new_trigger_price_P_input.setText("")
        self.new_buy_stop_offset_input.setText(str(self.default_trigger_params["buy_stop_offset_pips"]))
        self.new_sell_stop_offset_input.setText(str(self.default_trigger_params["sell_stop_offset_pips"]))
        self.new_triggered_lot_input.setText(str(self.default_trigger_params["triggered_orders_lot_size"]))
        self.new_triggered_tp_input.setText(str(self.default_trigger_params["triggered_orders_tp_pips"]))
        self.new_triggered_sl_input.setText(str(self.default_trigger_params["triggered_orders_sl_pips"]))

    def update_trigger_offset_visibility(self):
        """Ẩn/hiện các ô offset tùy theo loại lệnh trigger."""
        selected_type = self.trigger_order_type_combo.currentText()

        is_buy_visible = selected_type in ["Buy Stop", "Double Stop"]
        is_sell_visible = selected_type in ["Sell Stop", "Double Stop"]

        self.buy_offset_label.setVisible(is_buy_visible)
        self.new_buy_stop_offset_input.setVisible(is_buy_visible)

        self.sell_offset_label.setVisible(is_sell_visible)
        self.new_sell_stop_offset_input.setVisible(is_sell_visible)

    def update_price_input_state(self):
        """Cập nhật trạng thái của trường nhập giá dựa trên loại lệnh đã chọn."""
        order_type_text = self.pending_type_combo.currentText()
        if order_type_text in ["Buy", "Sell"]:
            self.pending_price_input.setEnabled(False)
            self.pending_price_input.setPlaceholderText("Không cần nhập cho lệnh thị trường")
            self.pending_price_input.clear()
        else:
            self.pending_price_input.setEnabled(True)
            self.pending_price_input.setPlaceholderText("Nhập giá đặt cho lệnh chờ")

    def append_log(self, message):
        """Thêm tin nhắn vào vùng log trên GUI."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_area.append(f"[{timestamp}] {message}")
        self.log_area.verticalScrollBar().setValue(self.log_area.verticalScrollBar().maximum())

    def connect_mt5(self):
        """Kết nối đến tài khoản MetaTrader 5."""
        try:
            account = int(self.account_input.text())
            password = self.password_input.text()
            server = self.server_input.text()
            update_interval = float(self.update_interval_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Lỗi", f"Thông tin nhập không hợp lệ (số/chuỗi): {e}")
            return

        if self.mt5_connected:
            self.append_log("Đang ngắt kết nối MT5 cũ...")
            self.protector_thread.connected = False
            time.sleep(0.5)

            if mt5.initialize():
                mt5.shutdown()
            self.mt5_connected = False
            self.place_order_btn.setEnabled(False)
            self.modify_pending_btn.setEnabled(False)
            self.cancel_pending_btn.setEnabled(False)
            self.breakeven_checkbox.setEnabled(False)
            self.breakeven_checkbox.setChecked(False)
            self.eod_cleanup_checkbox.setEnabled(False)
            self.eod_countdown_timer.stop()
            self.eod_countdown_label.setVisible(False)
            self.close_all_positions_btn.setEnabled(False)
            self.add_trigger_btn.setEnabled(False)
            self.remove_selected_trigger_btn.setEnabled(False)
            self.clear_all_triggers_btn.setEnabled(False)

            self.append_log("Đã ngắt kết nối MT5 cũ.")
            time.sleep(0.5)


        if not mt5.initialize():
            QMessageBox.warning(self, "Lỗi", "Không thể khởi tạo MetaTrader5. Đảm bảo MT5 terminal đang chạy.")
            return

        if not mt5.login(account, password=password, server=server):
            QMessageBox.warning(self, "Lỗi", f"Đăng nhập MT5 thất bại. Kiểm tra tài khoản, mật khẩu, server. Mã lỗi: {mt5.last_error()}")
            mt5.shutdown()
            return

        self.mt5_connected = True
        self.protector_thread.connected = True
        self.append_log(f"Kết nối MT5 tài khoản {account} thành công!")

        self.place_order_btn.setEnabled(True)
        self.modify_pending_btn.setEnabled(True)
        self.cancel_pending_btn.setEnabled(True)
        self.breakeven_checkbox.setEnabled(True)
        self.eod_cleanup_checkbox.setEnabled(True)
        self.close_all_positions_btn.setEnabled(True)
        self.add_trigger_btn.setEnabled(True)
        self.remove_selected_trigger_btn.setEnabled(True)
        self.clear_all_triggers_btn.setEnabled(True)

        self._global_params['update_interval'] = update_interval
        self.protector_thread.update_global_params(self._global_params)

        self.refresh_pending_orders()

    def on_breakeven_toggle(self, state):
        """Xử lý sự kiện bật/tắt checkbox Breakeven Protector."""
        if not self.mt5_connected:
            QMessageBox.warning(self, "Lỗi", "Bạn cần kết nối MT5 trước khi bật/tắt breakeven!")
            self.breakeven_checkbox.blockSignals(True)
            self.breakeven_checkbox.setChecked(False)
            self.breakeven_checkbox.blockSignals(False)
            return

        try:
            self._global_params['break_even_pips'] = float(self.be_pips_input.text())
            self._global_params['break_even_offset'] = float(self.be_offset_input.text())
            self._global_params['max_loss_per_day'] = float(self.max_loss_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Lỗi", f"Vui lòng nhập số hợp lệ cho cài đặt breakeven/lỗ tối đa: {e}")
            self.breakeven_checkbox.blockSignals(True)
            self.breakeven_checkbox.setChecked(False)
            self.breakeven_checkbox.blockSignals(False)
            return

        self.breakeven_on = state == Qt.Checked
        self.protector_thread.set_breakeven_on(self.breakeven_on)
        self.protector_thread.update_global_params(self._global_params)
        self.append_log(f"Chế độ bảo vệ breakeven: {'ON' if self.breakeven_on else 'OFF'}")

    def update_eod_countdown(self):
        """Cập nhật nhãn đồng hồ đếm ngược đến 00:00 UTC."""
        try:
            now_utc = datetime.now(pytz.utc)
            tomorrow_utc_date = now_utc.date() + timedelta(days=1)
            next_midnight_utc = datetime.combine(tomorrow_utc_date, datetime.min.time(), tzinfo=pytz.utc)
            time_left = next_midnight_utc - now_utc
            total_seconds = int(time_left.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            seconds = total_seconds % 60
            countdown_text = f" (còn {hours:02}:{minutes:02}:{seconds:02})"
            self.eod_countdown_label.setText(countdown_text)
        except Exception:
            self.eod_countdown_label.setText(" (Lỗi giờ)")

    def on_eod_cleanup_toggle(self, state):
        """Xử lý sự kiện bật/tắt checkbox dọn dẹp cuối ngày."""
        if not self.mt5_connected:
            QMessageBox.warning(self, "Lỗi", "Bạn cần kết nối MT5 trước khi bật/tắt chức năng này!")
            self.eod_cleanup_checkbox.blockSignals(True)
            self.eod_cleanup_checkbox.setChecked(False)
            self.eod_cleanup_checkbox.blockSignals(False)
            return

        is_on = state == Qt.Checked
        self._global_params['close_all_at_day_end'] = is_on
        self.protector_thread.update_global_params(self._global_params)
        self.append_log(f"Chức năng dọn dẹp cuối ngày (UTC): {'BẬT' if is_on else 'TẮT'}")

        if is_on:
            self.update_eod_countdown() # Cập nhật ngay lập tức
            self.eod_countdown_label.setVisible(True)
            self.eod_countdown_timer.start(1000) # Cập nhật mỗi giây
        else:
            self.eod_countdown_timer.stop()
            self.eod_countdown_label.setVisible(False)
            
    ## NEW: Helper method để tránh lặp code kiểm tra Symbol
    def _validate_and_get_symbol_info(self, symbol_raw):
        """Kiểm tra symbol, thử thêm hậu tố 'm', làm hiển thị và trả về symbol_info hợp lệ."""
        if not symbol_raw:
            QMessageBox.warning(self, "Lỗi", "Vui lòng nhập 'Cặp tiền (Symbol)'.")
            return None, None

        current_symbol = symbol_raw
        symbol_info = mt5.symbol_info(current_symbol)

        # Nếu symbol gốc không tồn tại, thử thêm hậu tố 'm'
        if symbol_info is None:
            self.logger.log(f"Symbol '{current_symbol}' không tồn tại. Thử thêm hậu tố 'm'.")
            potential_symbol = symbol_raw + "m"
            symbol_info_potential = mt5.symbol_info(potential_symbol)
            if symbol_info_potential is not None:
                current_symbol = potential_symbol
                symbol_info = symbol_info_potential
                self.logger.log(f"Đã tìm thấy symbol với hậu tố: '{current_symbol}'.")
            else:
                msg = f"Symbol '{symbol_raw}' và '{potential_symbol}' đều không tồn tại."
                self.append_log(f"Lỗi: {msg}")
                QMessageBox.warning(self, "Lỗi Symbol", msg)
                return None, None
        
        # Nếu symbol đã xác định nhưng chưa hiển thị, cố gắng làm hiển thị
        if not symbol_info.visible:
            self.logger.log(f"Symbol '{current_symbol}' không hiển thị, đang thử kích hoạt...")
            if not mt5.symbol_select(current_symbol, True):
                msg = f"Không thể làm hiển thị symbol '{current_symbol}'."
                self.append_log(f"Lỗi: {msg}")
                QMessageBox.warning(self, "Lỗi Symbol", msg)
                return None, None
            # Lấy lại thông tin sau khi làm hiển thị, vì nó có thể đã thay đổi
            symbol_info = mt5.symbol_info(current_symbol)
            if symbol_info is None:
                msg = f"Lấy lại thông tin symbol '{current_symbol}' sau khi làm hiển thị thất bại."
                self.append_log(f"Lỗi: {msg}")
                QMessageBox.warning(self, "Lỗi Symbol", msg)
                return None, None
        
        return current_symbol, symbol_info

    def add_new_trigger(self):
        """Thêm một lệnh kích hoạt mới vào danh sách theo dõi."""
        if not self.mt5_connected:
            QMessageBox.warning(self, "Lỗi", "Bạn cần kết nối MT5 trước!")
            return
        try:
            symbol_raw = self.new_trigger_symbol_input.text().strip().upper()
            price_P = float(self.new_trigger_price_P_input.text())
            buy_stop_offset = float(self.new_buy_stop_offset_input.text())
            sell_stop_offset = float(self.new_sell_stop_offset_input.text())
            lot = float(self.new_triggered_lot_input.text())
            tp = float(self.new_triggered_tp_input.text())
            sl = float(self.new_triggered_sl_input.text())
            order_type = self.trigger_order_type_combo.currentText()

            # --- Validation ---
            if price_P <= 0 or lot <= 0 or buy_stop_offset <= 0 or sell_stop_offset <= 0:
                QMessageBox.warning(self, "Lỗi", "Giá trị Giá P, Lot, và Offset phải lớn hơn 0.")
                return

            ## NEW: Sử dụng helper method để kiểm tra symbol
            current_symbol, symbol_info = self._validate_and_get_symbol_info(symbol_raw)
            if not symbol_info:
                return
            
            # Cập nhật lại ô input nếu symbol đã được thay đổi (thêm 'm')
            self.new_trigger_symbol_input.setText(current_symbol)
            
            # Kiểm tra Lot size theo min/max/step của symbol
            if lot < symbol_info.volume_min or lot > symbol_info.volume_max or \
               round((lot - symbol_info.volume_min) % symbol_info.volume_step, 5) != 0:
                QMessageBox.warning(self, "Lỗi Lot",
                                    f"Lot size không hợp lệ cho {current_symbol}.\n"
                                    f"Min: {symbol_info.volume_min}, Max: {symbol_info.volume_max}, Step: {symbol_info.volume_step}")
                return

            trigger_config = {
                "symbol": current_symbol, "price_P": price_P,
                "buy_stop_offset_pips": buy_stop_offset, "sell_stop_offset_pips": sell_stop_offset,
                "triggered_orders_lot_size": lot, "triggered_orders_tp_pips": tp,
                "triggered_orders_sl_pips": sl, "order_type": order_type
            }
            self.protector_thread.add_trigger(trigger_config)

        except ValueError as e:
            QMessageBox.warning(self, "Lỗi", f"Vui lòng nhập số hợp lệ cho cài đặt lệnh: {e}")
        except Exception as e:
            self.append_log(f"Lỗi không xác định khi thêm lệnh kích hoạt: {e}")
            QMessageBox.critical(self, "Lỗi", f"Có lỗi xảy ra: {e}")

    def remove_selected_trigger(self):
        """Xóa lệnh kích hoạt được chọn từ bảng theo dõi."""
        selected_rows = self.trigger_monitor_table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.warning(self, "Chọn lệnh", "Vui lòng chọn một lệnh kích hoạt để xóa.")
            return

        row = selected_rows[0].row()
        trigger_id = int(self.trigger_monitor_table.item(row, 0).text())
        symbol_to_remove = self.trigger_monitor_table.item(row, 1).text()

        reply = QMessageBox.question(self, "Xác nhận Xóa",
                                     f"Bạn có chắc chắn muốn xóa lệnh kích hoạt '{symbol_to_remove}' (ID: {trigger_id}) không?",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.protector_thread.remove_trigger(trigger_id)

    def clear_all_triggers(self):
        """Xóa tất cả các lệnh kích hoạt đang được theo dõi."""
        reply = QMessageBox.question(self, "Xác nhận Xóa Tất Cả",
                                     "Bạn có chắc chắn muốn xóa TẤT CẢ các lệnh kích hoạt đang được theo dõi không?",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.protector_thread.clear_all_triggers()

    def place_order(self):
        """Đặt lệnh thị trường hoặc lệnh chờ mới."""
        if not self.mt5_connected:
            QMessageBox.warning(self, "Lỗi", "Bạn cần kết nối MT5 trước khi vào lệnh!")
            return
        try:
            symbol_raw = self.symbol_input.text().strip().upper()
            lot = float(self.pending_lot_input.text())
            tp_pips = float(self.tp_pips_input.text()) if self.tp_pips_input.text() else 0.0
            sl_pips = float(self.sl_pips_input.text()) if self.sl_pips_input.text() else 0.0

            order_type_text = self.pending_type_combo.currentText()
            order_type_map = {
                "Buy": mt5.ORDER_TYPE_BUY, "Sell": mt5.ORDER_TYPE_SELL,
                "Buy Limit": mt5.ORDER_TYPE_BUY_LIMIT, "Sell Limit": mt5.ORDER_TYPE_SELL_LIMIT,
                "Buy Stop": mt5.ORDER_TYPE_BUY_STOP, "Sell Stop": mt5.ORDER_TYPE_SELL_STOP
            }
            order_type = order_type_map[order_type_text]

            price = None
            if order_type_text not in ['Buy', 'Sell']:
                if not self.pending_price_input.text():
                    QMessageBox.warning(self, "Lỗi", "Bạn cần nhập giá đặt cho lệnh chờ.")
                    return
                price = float(self.pending_price_input.text())

            ## NEW: Sử dụng helper method để kiểm tra symbol
            current_symbol, symbol_info = self._validate_and_get_symbol_info(symbol_raw)
            if not symbol_info:
                return

            # Kiểm tra Lot size
            if lot < symbol_info.volume_min or lot > symbol_info.volume_max or \
               round((lot - symbol_info.volume_min) % symbol_info.volume_step, 5) != 0:
                QMessageBox.warning(self, "Lỗi Lot", f"Lot size không hợp lệ cho {current_symbol}.")
                return

            pip_step = (10 ** -symbol_info.digits) * (10 if "JPY" not in current_symbol else 1000)

            current_tick = mt5.symbol_info_tick(current_symbol)
            if not current_tick:
                QMessageBox.warning(self, "Lỗi Tick Data", f"Không thể lấy tick data cho '{current_symbol}'.")
                return

            request = {
                "symbol": current_symbol, "volume": lot, "type": order_type,
                "magic": self.MANUAL_ORDER_MAGIC, "comment": f"Manual {order_type_text}",
                "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
            }

            if order_type_text in ['Buy', 'Sell']:
                price_exec = current_tick.ask if order_type_text == "Buy" else current_tick.bid
                tp = price_exec + tp_pips * pip_step if tp_pips > 0 else 0.0
                sl = price_exec - sl_pips * pip_step if sl_pips > 0 else 0.0
                if order_type_text == "Sell":
                    tp = price_exec - tp_pips * pip_step if tp_pips > 0 else 0.0
                    sl = price_exec + sl_pips * pip_step if sl_pips > 0 else 0.0
                
                request.update({
                    "action": mt5.TRADE_ACTION_DEAL, "price": round(price_exec, symbol_info.digits),
                    "sl": round(sl, symbol_info.digits), "tp": round(tp, symbol_info.digits), "deviation": 20
                })
            else: # Lệnh chờ
                tp = price + tp_pips * pip_step if tp_pips > 0 else 0.0
                sl = price - sl_pips * pip_step if sl_pips > 0 else 0.0
                if order_type_text in ["Sell Limit", "Sell Stop"]:
                    tp = price - tp_pips * pip_step if tp_pips > 0 else 0.0
                    sl = price + sl_pips * pip_step if sl_pips > 0 else 0.0

                request.update({
                    "action": mt5.TRADE_ACTION_PENDING, "price": round(price, symbol_info.digits),
                    "sl": round(sl, symbol_info.digits), "tp": round(tp, symbol_info.digits)
                })

            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                self.append_log(f"Đã gửi lệnh {order_type_text} {current_symbol} thành công! Ticket: {result.order}")
                self.refresh_pending_orders()
            else:
                error_msg = mt5.last_error()
                self.append_log(f"Lỗi gửi lệnh: {result.retcode if result else 'None'} ({error_msg})")
                QMessageBox.warning(self, "Lỗi Gửi Lệnh", f"Gửi lệnh thất bại: {result.comment if result else ''} (Mã: {result.retcode if result else 'N/A'})\nDetails: {error_msg}")

        except ValueError as e:
            QMessageBox.warning(self, "Lỗi nhập liệu", f"Vui lòng nhập số hợp lệ: {e}")
        except Exception as e:
            self.append_log(f"Lỗi không xác định khi vào lệnh: {e}")
            QMessageBox.critical(self, "Lỗi", f"Có lỗi xảy ra: {e}")

    def refresh_pending_orders(self):
        """Cập nhật bảng các lệnh chờ đang hoạt động."""
        if not self.mt5_connected:
            self.pending_orders_table.setRowCount(0)
            return
        orders = mt5.orders_get()
        self.pending_orders_table.setRowCount(0)

        triggered_orders_P_price = self.protector_thread.triggered_orders_P_price

        for order in orders or []:
            row = self.pending_orders_table.rowCount()
            self.pending_orders_table.insertRow(row)

            symbol_info = mt5.symbol_info(order.symbol)
            digits = symbol_info.digits if symbol_info else 5

            self.pending_orders_table.setItem(row, 0, QTableWidgetItem(str(order.ticket)))
            self.pending_orders_table.setItem(row, 1, QTableWidgetItem(order.symbol))

            type_name_map = {
                mt5.ORDER_TYPE_BUY_LIMIT: "Buy Limit", mt5.ORDER_TYPE_SELL_LIMIT: "Sell Limit",
                mt5.ORDER_TYPE_BUY_STOP: "Buy Stop", mt5.ORDER_TYPE_SELL_STOP: "Sell Stop",
            }
            type_name = type_name_map.get(order.type, f"Unknown ({order.type})")
            self.pending_orders_table.setItem(row, 2, QTableWidgetItem(type_name))

            self.pending_orders_table.setItem(row, 3, QTableWidgetItem(f"{order.price_open:.{digits}f}"))
            self.pending_orders_table.setItem(row, 4, QTableWidgetItem(str(order.volume_initial)))
            self.pending_orders_table.setItem(row, 5, QTableWidgetItem(f"{order.tp:.{digits}f}" if order.tp > 0 else "0.0"))
            self.pending_orders_table.setItem(row, 6, QTableWidgetItem(f"{order.sl:.{digits}f}" if order.sl > 0 else "0.0"))

            price_p_display = "N/A"
            if order.magic in [self.TRIGGER_BUY_MAGIC, self.TRIGGER_SELL_MAGIC] and order.ticket in triggered_orders_P_price:
                price_p = triggered_orders_P_price[order.ticket]
                price_p_display = f"{price_p:.{digits}f}"
            self.pending_orders_table.setItem(row, 7, QTableWidgetItem(price_p_display))


        self.pending_orders_table.resizeColumnsToContents()
        self.pending_orders_table.horizontalHeader().setStretchLastSection(True)

    def update_trigger_monitor_table(self, trigger_data_list):
        """Cập nhật bảng theo dõi lệnh kích hoạt trên GUI."""
        self.trigger_monitor_table.setRowCount(0)
        for data in trigger_data_list:
            row = self.trigger_monitor_table.rowCount()
            self.trigger_monitor_table.insertRow(row)

            symbol_info = mt5.symbol_info(data['symbol'])
            digits = symbol_info.digits if symbol_info else 5

            self.trigger_monitor_table.setItem(row, 0, QTableWidgetItem(str(data['id'])))
            self.trigger_monitor_table.setItem(row, 1, QTableWidgetItem(data['symbol']))
            self.trigger_monitor_table.setItem(row, 2, QTableWidgetItem(f"{data['price_P']:.{digits}f}"))

            current_price_item = QTableWidgetItem(f"{data['current_price']:.{digits}f}" if data['current_price'] != 0.0 else "N/A")
            self.trigger_monitor_table.setItem(row, 3, current_price_item)

            status_item = QTableWidgetItem(data['status'])
            self.trigger_monitor_table.setItem(row, 4, status_item)

            order_type_item = QTableWidgetItem(data.get('order_type', 'N/A'))
            self.trigger_monitor_table.setItem(row, 5, order_type_item)

        self.trigger_monitor_table.resizeColumnsToContents()
        self.trigger_monitor_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

    def update_open_positions_table(self, positions_data):
        """Cập nhật bảng các lệnh đang mở trên GUI."""
        self.open_positions_table.setRowCount(0)
        for pos_data in positions_data:
            row = self.open_positions_table.rowCount()
            self.open_positions_table.insertRow(row)

            self.open_positions_table.setItem(row, 0, QTableWidgetItem(str(pos_data['ticket'])))
            self.open_positions_table.setItem(row, 1, QTableWidgetItem(pos_data['symbol']))

            pips_item = QTableWidgetItem(f"{pos_data['profit_pips']:.2f}")
            pips_item.setForeground(Qt.darkGreen if pos_data['profit_pips'] >= 0 else Qt.red)
            self.open_positions_table.setItem(row, 2, pips_item)

            usd_item = QTableWidgetItem(f"{pos_data['profit_usd']:.2f}")
            usd_item.setForeground(Qt.darkGreen if pos_data['profit_usd'] >= 0 else Qt.red)
            self.open_positions_table.setItem(row, 3, usd_item)

        self.open_positions_table.resizeColumnsToContents()
        self.open_positions_table.horizontalHeader().setStretchLastSection(True)

    def modify_pending_order(self):
        """Sửa đổi các thuộc tính của lệnh chờ đã chọn."""
        if not self.mt5_connected:
            QMessageBox.warning(self, "Lỗi", "Bạn cần kết nối MT5!")
            return
        try:
            selected_rows = self.pending_orders_table.selectionModel().selectedRows()
            if not selected_rows:
                QMessageBox.warning(self, "Chọn lệnh", "Chọn một lệnh để sửa.")
                return

            row = selected_rows[0].row()
            ticket = int(self.pending_orders_table.item(row, 0).text())
            symbol = self.pending_orders_table.item(row, 1).text()

            symbol_info = mt5.symbol_info(symbol)
            if not symbol_info:
                QMessageBox.warning(self, "Lỗi", f"Không thể lấy thông tin symbol cho {symbol}.")
                return

            orders = mt5.orders_get(ticket=ticket)
            if not orders:
                QMessageBox.warning(self, "Lỗi", f"Không tìm thấy lệnh chờ {ticket}.")
                self.refresh_pending_orders()
                return
            current_order = orders[0]
            
            # Chỉ cập nhật các giá trị được người dùng nhập vào
            request = {"action": mt5.TRADE_ACTION_MODIFY, "order": ticket}
            
            if self.pending_price_input.text():
                request["price"] = round(float(self.pending_price_input.text()), symbol_info.digits)
            if self.pending_lot_input.text():
                 request["volume"] = float(self.pending_lot_input.text())

            # Tính toán lại TP/SL nếu người dùng nhập pips
            base_price = request.get("price", current_order.price_open)
            pip_step = (10 ** -symbol_info.digits) * (10 if "JPY" not in symbol else 1000)

            if self.tp_pips_input.text():
                tp_pips = float(self.tp_pips_input.text())
                tp_val = base_price + tp_pips * pip_step if current_order.type in [mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP] else base_price - tp_pips * pip_step
                request["tp"] = round(tp_val, symbol_info.digits)
            
            if self.sl_pips_input.text():
                sl_pips = float(self.sl_pips_input.text())
                sl_val = base_price - sl_pips * pip_step if current_order.type in [mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP] else base_price + sl_pips * pip_step
                request["sl"] = round(sl_val, symbol_info.digits)

            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                self.append_log(f"Sửa lệnh chờ {ticket} thành công.")
            else:
                self.append_log(f"Lỗi sửa lệnh chờ {ticket}: {result.retcode if result else 'None'} ({mt5.last_error()})")
                QMessageBox.warning(self, "Lỗi Sửa Lệnh", f"Sửa lệnh thất bại: {result.comment} (Mã lỗi: {result.retcode})")
            self.refresh_pending_orders()

        except ValueError as e:
            QMessageBox.warning(self, "Lỗi nhập liệu", f"Vui lòng nhập số hợp lệ: {e}")
        except Exception as e:
            self.append_log(f"Lỗi không xác định khi sửa lệnh chờ: {e}")
            QMessageBox.critical(self, "Lỗi", f"Có lỗi xảy ra: {e}")

    def cancel_pending_order(self):
        """Huỷ lệnh chờ đã chọn."""
        if not self.mt5_connected:
            QMessageBox.warning(self, "Lỗi", "Bạn cần kết nối MT5!")
            return
        try:
            selected_rows = self.pending_orders_table.selectionModel().selectedRows()
            if not selected_rows:
                QMessageBox.warning(self, "Chọn lệnh", "Chọn một lệnh để hủy.")
                return

            row = selected_rows[0].row()
            ticket = int(self.pending_orders_table.item(row, 0).text())

            reply = QMessageBox.question(self, "Xác nhận Hủy",
                                         f"Bạn có chắc chắn muốn hủy lệnh chờ {ticket} không?",
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.No:
                return

            request = {
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": ticket,
                "comment": "Cancel pending order from GUI"
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                self.append_log(f"Đã huỷ lệnh chờ {ticket} thành công.")
                if ticket in self.protector_thread.triggered_orders_P_price:
                    del self.protector_thread.triggered_orders_P_price[ticket]
            else:
                self.append_log(f"Lỗi huỷ lệnh chờ {ticket}: {result.retcode if result else 'None'} ({mt5.last_error()})")
                QMessageBox.warning(self, "Lỗi Hủy Lệnh", f"Hủy lệnh thất bại: {result.comment} (Mã lỗi: {result.retcode})")
            self.refresh_pending_orders()

        except Exception as e:
            self.append_log(f"Lỗi khi huỷ lệnh chờ: {e}")
            QMessageBox.critical(self, "Lỗi", f"Có lỗi xảy ra: {e}")

    def close_all_open_positions(self):
        """Đóng tất cả các lệnh đang mở."""
        if not self.mt5_connected:
            QMessageBox.warning(self, "Lỗi", "Bạn cần kết nối MT5!")
            return

        reply = QMessageBox.question(self, "Xác nhận Đóng tất cả",
                                     "Bạn có chắc chắn muốn đóng TẤT CẢ các lệnh đang mở không?",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.No:
            return

        positions = mt5.positions_get()
        if not positions:
            QMessageBox.information(self, "Thông báo", "Không có lệnh nào đang mở để đóng.")
            return

        closed_count, failed_count = 0, 0
        for pos in positions:
            tick = mt5.symbol_info_tick(pos.symbol)
            if not tick:
                self.append_log(f"Lỗi: Không thể lấy tick data cho '{pos.symbol}' để đóng lệnh {pos.ticket}.")
                failed_count += 1
                continue

            request = {
                "action": mt5.TRADE_ACTION_DEAL, "position": pos.ticket, "symbol": pos.symbol,
                "volume": pos.volume, "deviation": 20, "magic": pos.magic,
                "comment": "Close all from GUI", "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            if pos.type == mt5.ORDER_TYPE_BUY:
                request["type"] = mt5.ORDER_TYPE_SELL
                request["price"] = tick.bid
            elif pos.type == mt5.ORDER_TYPE_SELL:
                request["type"] = mt5.ORDER_TYPE_BUY
                request["price"] = tick.ask
            else:
                failed_count += 1
                continue

            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                self.append_log(f"Đã đóng lệnh {pos.ticket} thành công.")
                closed_count += 1
            else:
                self.append_log(f"Lỗi đóng lệnh {pos.ticket}: {result.retcode if result else 'None'} ({mt5.last_error()})")
                failed_count += 1

        self.append_log(f"Hoàn tất: Đã đóng {closed_count} lệnh, thất bại {failed_count} lệnh.")
        QMessageBox.information(self, "Kết quả", f"Hoàn tất: Đã đóng {closed_count} lệnh, thất bại {failed_count} lệnh.")

    def reset_program(self):
        """Đặt lại chương trình về trạng thái ban đầu."""
        reply = QMessageBox.question(self, "Xác nhận Reset",
                                     "Bạn có chắc muốn RESET chương trình về trạng thái ban đầu không?",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.No:
            return

        self.append_log("Đang reset chương trình...")

        if self.mt5_connected:
            self.protector_thread.connected = False
            time.sleep(0.5)
            if mt5.initialize():
                mt5.shutdown()
            self.mt5_connected = False
            self.append_log("Đã ngắt kết nối MT5.")

        self.protector_thread.stop()
        self.protector_thread.join(timeout=2)
        
        self._global_params = self.default_global_params.copy()
        self.protector_thread = BreakevenProtector(self.logger, self._global_params, self.constants)
        self.protector_thread.signals.position_update_signal.connect(self.update_open_positions_table)
        self.protector_thread.signals.trigger_monitor_update_signal.connect(self.update_trigger_monitor_table)
        self.protector_thread.start()
        self.append_log("Breakeven Protector thread đã được khởi tạo lại.")

        self.apply_default_settings()

        self.place_order_btn.setEnabled(False)
        self.modify_pending_btn.setEnabled(False)
        self.cancel_pending_btn.setEnabled(False)
        self.breakeven_checkbox.setEnabled(False)
        self.breakeven_checkbox.setChecked(False)
        self.eod_cleanup_checkbox.setEnabled(False)
        self.eod_cleanup_checkbox.setChecked(False)
        self.breakeven_on = False
        self.close_all_positions_btn.setEnabled(False)
        self.add_trigger_btn.setEnabled(False)
        self.remove_selected_trigger_btn.setEnabled(False)
        self.clear_all_triggers_btn.setEnabled(False)

        self.open_positions_table.setRowCount(0)
        self.pending_orders_table.setRowCount(0)
        self.trigger_monitor_table.setRowCount(0)

        self.log_area.clear()
        self.append_log("Chương trình đã được reset về trạng thái ban đầu.")
        QMessageBox.information(self, "Hoàn tất", "Chương trình đã được reset.")

    def closeEvent(self, event):
        """Xử lý sự kiện đóng cửa sổ chính."""
        self.append_log("Đang đóng chương trình...")

        if self.protector_thread.is_alive():
            self.protector_thread.stop()
            self.protector_thread.join(timeout=5)

        if mt5.initialize():
            mt5.shutdown()
            self.logger.log("Đã ngắt kết nối MT5.")

        self.logger.log("Chương trình đã đóng.")
        super().closeEvent(event)


# --- Main Application Entry Point ---
def main():
    """Khởi chạy ứng dụng."""
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()