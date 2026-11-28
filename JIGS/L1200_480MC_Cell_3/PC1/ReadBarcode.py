import serial
import time

listCommand = {
    'scan_disable': "04 EA 04 00 FF 0E",
    'scan_enable': "04 E9 04 00 FF 0F",
    'reset': "04 FA 04 00 FE FE",
    'volume_high': "07 C6 04 08 00 8C 00 FE 9B",
    'volume_low': "07 C6 04 08 00 8C 02 FE 99",
    'manual_mode': "07 C6 04 08 00 8A 00 FE 9D",
    'auto_mode': "07 C6 04 08 00 8A 09 FE 94",
    'continues_mode': "07 C6 04 08 00 8A 04 FE 99",
    'onkey_mode': "07 C6 04 08 00 8A 02 FE 9B",
}

class ScanHome:
    def __init__(self, com=None, baud_rate=9600):
        self.com = com
        self.baudRate = baud_rate
        self.serial = None
        self.command = listCommand
        self.barcode = None
        self.response = None
        self.rx_buffer = b''

    def getSerial(self):
        if self.com is None:
            print("❌ Không có cổng COM.")
            return None

        if self.serial is not None and self.serial.is_open:
            return self.serial

        try:
            self.serial = serial.Serial(
                port=self.com,
                baudrate=self.baudRate,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                bytesize=serial.EIGHTBITS,
                timeout=0.1
            )
            print(f"✅ Đã kết nối {self.com} - {self.baudRate} baud.")
            return self.serial

        except serial.SerialException as e:
            print(f"❌ Lỗi Serial: {e}")
            self.serial = None
            return None

        except Exception as e:
            print(f"❌ Lỗi không xác định: {e}")
            self.serial = None
            return None

    def close(self):
        if self.serial and self.serial.is_open:
            try:
                self.serial.close()
                print(f"Đã đóng {self.com}.")
            except Exception as e:
                print(f"⚠️ Lỗi đóng Serial: {e}")

        self.serial = None

    def getKey(self, command):
        key = next(
            (k for k, v in self.command.items() if v == command),
            None
        )

        if key is None:
            return None

        return key.replace('_', ' ').capitalize()

    def send(self, command):
        if not command:
            print("❌ Command rỗng.")
            return False

        if command in self.command.values():
            commandData = command
        else:
            commandFix = str(command).strip().replace(' ', '_').lower()
            commandData = self.command.get(commandFix)

        if commandData is None:
            print(f"❌ Không tìm thấy command: {command}")
            return False

        if not self.getSerial():
            return False

        try:
            hexBytes = hex_to_bytes(commandData)
            keyCommand = self.getKey(commandData)

            if keyCommand:
                print(f"Gửi câu lệnh {keyCommand}")

            print(f"HEX : {commandData}")

            self.serial.write(hexBytes)
            self.serial.flush()

            return True

        except Exception as e:
            print(f"❌ Lỗi gửi command: {e}")
            return False

    def clearBuffer(self):
        """
        Xóa toàn bộ dữ liệu cũ.

        QUAN TRỌNG:
        Chỉ gọi trước scan_enable.
        Không gọi sau scan_enable.
        """
        if not self.getSerial():
            return False

        try:
            waiting = self.serial.in_waiting

            print(
                f"[CLEAR BUFFER] "
                f"in_waiting trước clear = {waiting}"
            )

            self.serial.reset_input_buffer()
            self.rx_buffer = b''
            self.barcode = None

            print(
                f"[CLEAR BUFFER] "
                f"in_waiting sau clear = {self.serial.in_waiting}"
            )

            return True

        except Exception as e:
            print(f"❌ Lỗi clear buffer: {e}")
            return False

    def readResponse(self, wait=0.2):
        """
        Chỉ dùng để debug command.

        KHÔNG gọi giữa scan_enable và readBarcode().
        """
        if not self.getSerial():
            return None

        time.sleep(wait)

        try:
            waiting = self.serial.in_waiting

            if waiting <= 0:
                print("Không có response.")
                return None

            self.response = self.serial.read(waiting)

            print(f"Response Bytes: {self.response}")
            print(
                f"Response HEX  : "
                f"{self.response.hex(' ').upper()}"
            )

            return self.response

        except Exception as e:
            print(f"❌ Lỗi đọc response: {e}")
            return None

    def getResponse(self):
        return self.response if self.response else None

    def _extractBarcode(self, frame):
        if not frame:
            return None

        print(
            f"FRAME HEX : "
            f"{frame.hex(' ').upper()}"
        )

        # Chỉ lấy ký tự ASCII có thể hiển thị
        printable = bytes(
            b for b in frame
            if 0x20 <= b <= 0x7E
        )

        text = printable.decode(
            'ascii',
            errors='ignore'
        ).strip()

        print(f"Printable : {repr(text)}")

        if not text:
            return None

        # Protocol scanner của bạn:
        # 04 D0 00 00 FF 2C BARCODE 0D
        #
        # 0x2C = dấu ,
        #
        # Printable sẽ thành:
        # ,VNF2202270

        if ',' in text:
            text = text.split(',')[-1].strip()

        text = text.strip(
            '\r\n\t\x00 '
        )

        if not text:
            return None

        # Barcode của bạn dạng:
        # VNF2202270
        #
        # Cho phép chữ + số + một số ký tự thông dụng
        allowed = set(
            'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
            'abcdefghijklmnopqrstuvwxyz'
            '0123456789'
            '-_.'
        )

        if not all(c in allowed for c in text):
            print(
                f"⚠️ Barcode không hợp lệ: "
                f"{repr(text)}"
            )
            return None

        return text

    def _processRxBuffer(self):
        """
        Xử lý dữ liệu đã nằm trong rx_buffer.

        Scanner kết thúc barcode bằng 0D (CR).
        """

        while b'\r' in self.rx_buffer:
            frame, self.rx_buffer = self.rx_buffer.split(
                b'\r',
                1
            )

            barcode = self._extractBarcode(frame)

            if barcode:
                return barcode

        return None

    def readBarcode(self, timeout=15):
        """
        Chờ barcode MỚI.

        Hàm này KHÔNG clear COM buffer.
        Main phải clearBuffer() trước scan_enable.
        """

        if not self.getSerial():
            print("❌ Không kết nối được scanner.")
            return None

        # Không được trả barcode cũ
        self.barcode = None

        print(
            f"🔎 Chờ barcode mới "
            f"({timeout}s)..."
        )

        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                # --------------------------------
                # 1. Xử lý dữ liệu đã có trong
                #    rx_buffer trước
                # --------------------------------

                barcode = self._processRxBuffer()

                if barcode:
                    self.barcode = barcode

                    print(
                        f"✅ BARCODE NEW: "
                        f"{self.barcode}"
                    )

                    return self.barcode

                # --------------------------------
                # 2. Đọc dữ liệu mới từ COM
                # --------------------------------

                waiting = self.serial.in_waiting

                if waiting > 0:
                    raw_data = self.serial.read(waiting)

                    if raw_data:
                        print(
                            f"RAW HEX : "
                            f"{raw_data.hex(' ').upper()}"
                        )

                        self.rx_buffer += raw_data

                        # Sau khi nhận dữ liệu mới
                        # xử lý ngay
                        barcode = self._processRxBuffer()

                        if barcode:
                            self.barcode = barcode

                            print(
                                f"✅ BARCODE NEW: "
                                f"{self.barcode}"
                            )

                            return self.barcode

                time.sleep(0.01)

            except serial.SerialException as e:
                print(
                    f"❌ Serial error: {e}"
                )
                return None

            except Exception as e:
                print(
                    f"❌ Lỗi đọc barcode: {e}"
                )
                return None

        print(
            f"⏰ Timeout {timeout}s - "
            f"không có barcode mới."
        )

        # Quan trọng:
        # timeout thì vẫn None,
        # KHÔNG trả barcode lần trước
        self.barcode = None

        return None

    def getBarcode(self):
        return self.barcode if self.barcode else ""


def hex_to_bytes(hex_str):
    if hex_str is None:
        raise ValueError(
            "Chuỗi HEX đang là None."
        )

    hex_str = hex_str.replace(' ', '')

    if len(hex_str) % 2 != 0:
        raise ValueError(
            "Chuỗi HEX phải có độ dài chẵn."
        )

    return bytes.fromhex(hex_str)