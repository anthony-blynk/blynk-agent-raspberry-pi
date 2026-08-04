import sys
import paho.mqtt.client as mqtt


def load_token(path="blynk-data/blynk.env"):
    with open(path) as f:
        for line in f:
            if line.startswith("BLYNK_AUTH_TOKEN"):
                return line.split("=", 1)[1].strip()


TOKEN = load_token()
SERVER = "blynk.cloud"
PORT = 8883

protocol_arg = sys.argv[1] if len(sys.argv) > 1 else "v311"
protocol = mqtt.MQTTv5 if protocol_arg == "v5" else mqtt.MQTTv311


def on_connect(client, userdata, flags, reason_code, properties):
    print("CONNECT:", reason_code)
    client.subscribe("downlink/#")


def on_message(client, userdata, message):
    print(f"MESSAGE {message.topic}: {message.payload!r}")


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    print("DISCONNECT:", reason_code, properties)
    if properties is not None:
        server_ref = getattr(properties, "ServerReference", None)
        if server_ref:
            print("ServerReference:", server_ref)


client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, protocol=protocol)
client.username_pw_set("device", TOKEN)
client.tls_set()
client.on_connect = on_connect
client.on_message = on_message
client.on_disconnect = on_disconnect

print(f"Connecting with protocol={protocol_arg}")
client.connect(SERVER, PORT, keepalive=45)
client.loop_forever()
