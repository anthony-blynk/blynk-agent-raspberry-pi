import time
import paho.mqtt.client as mqtt

TOPIC = "ds/Test2"

client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
client.connect("localhost", 1883)
client.loop_start()

counter = 0
while True:
    client.publish(TOPIC, counter)
    print(f"{TOPIC}: {counter}")
    counter += 1
    time.sleep(3)
