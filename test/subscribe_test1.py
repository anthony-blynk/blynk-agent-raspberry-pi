import paho.mqtt.client as mqtt

TOPIC = "downlink/ds/Test1"


def on_connect(client, userdata, flags, reason_code, properties):
    print("Connected:", reason_code)
    client.subscribe(TOPIC)


def on_message(client, userdata, message):
    print(f"{message.topic}: {message.payload.decode()}")


client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message
client.connect("localhost", 1883)
client.loop_forever()
