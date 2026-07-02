#include <Wire.h>
void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 5000) {}  // wait for host to open port
  Wire.begin();
  delay(200);
  Serial.println("I2C scan:");
  for (byte addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.print("  Found: 0x");
      Serial.println(addr, HEX);
    }
  }
  Serial.println("Done.");
}
void loop() {}
