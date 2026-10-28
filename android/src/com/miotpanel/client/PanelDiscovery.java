package com.miotpanel.client;

import org.json.JSONObject;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.InterfaceAddress;
import java.net.NetworkInterface;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

public final class PanelDiscovery {

    public static final int DISCOVERY_PORT = 37020;

    public static class Panel {
        public String name;
        public String host;
        public String version;
        public int port;
        public boolean pin;

        public String label() {
            return name + "\n" + host + ":" + port + (pin ? "  ·  需要 PIN" : "");
        }

        public String key() {
            return host + ":" + port;
        }
    }

    public interface Listener {
        void onFound(Panel panel);
        void onFinished();
    }

    private PanelDiscovery() {
    }

    public static void discover(final Listener listener) {
        new Thread(new Runnable() {
            @Override
            public void run() {
                DatagramSocket socket = null;
                Set<String> seen = new HashSet<String>();
                try {
                    socket = new DatagramSocket();
                    socket.setBroadcast(true);
                    socket.setSoTimeout(600);
                    byte[] payload = "MIOT_DISCOVER 1".getBytes("UTF-8");
                    List<InetAddress> targets = broadcastAddresses();
                    long deadline = System.currentTimeMillis() + 4500;
                    while (System.currentTimeMillis() < deadline) {
                        for (int i = 0; i < targets.size(); i++) {
                            try {
                                socket.send(new DatagramPacket(payload, payload.length,
                                        targets.get(i), DISCOVERY_PORT));
                            } catch (Exception ignored) {
                            }
                        }
                        long roundEnd = Math.min(deadline, System.currentTimeMillis() + 1100);
                        while (System.currentTimeMillis() < roundEnd) {
                            try {
                                byte[] buffer = new byte[2048];
                                DatagramPacket packet = new DatagramPacket(buffer, buffer.length);
                                socket.receive(packet);
                                Panel panel = parse(packet);
                                if (panel != null && seen.add(panel.key())) {
                                    listener.onFound(panel);
                                }
                            } catch (SocketTimeoutException ignored) {
                            } catch (Exception ignored) {
                            }
                        }
                    }
                } catch (Exception ignored) {
                } finally {
                    if (socket != null) {
                        socket.close();
                    }
                    listener.onFinished();
                }
            }
        }, "miot-discovery").start();
    }

    public static void scanSubnet(final Listener listener) {
        new Thread(new Runnable() {
            @Override
            public void run() {
                final Set<String> seen = Collections.newSetFromMap(
                        new ConcurrentHashMap<String, Boolean>());
                final String prefix = localPrefix();
                if (prefix == null) {
                    listener.onFinished();
                    return;
                }
                ExecutorService pool = Executors.newFixedThreadPool(32);
                for (int i = 1; i <= 254; i++) {
                    final String host = prefix + i;
                    final int[] ports = new int[]{80, 8080};
                    for (int p = 0; p < ports.length; p++) {
                        final int port = ports[p];
                        pool.execute(new Runnable() {
                            @Override
                            public void run() {
                                try {
                                    Socket socket = new Socket();
                                    socket.connect(new InetSocketAddress(host, port), 180);
                                    socket.close();
                                    PanelClient.Probe probe = PanelClient.probe(host, port, null);
                                    if (!probe.found) {
                                        return;
                                    }
                                    Panel panel = new Panel();
                                    panel.host = host;
                                    panel.port = port;
                                    panel.name = host;
                                    panel.version = probe.version;
                                    panel.pin = probe.needsPin;
                                    if (seen.add(panel.key())) {
                                        listener.onFound(panel);
                                    }
                                } catch (Exception ignored) {
                                }
                            }
                        });
                    }
                }
                pool.shutdown();
                try {
                    pool.awaitTermination(25, TimeUnit.SECONDS);
                } catch (InterruptedException ignored) {
                    Thread.currentThread().interrupt();
                }
                listener.onFinished();
            }
        }, "miot-scan").start();
    }

    private static Panel parse(DatagramPacket packet) {
        try {
            String text = new String(packet.getData(), 0, packet.getLength(), "UTF-8");
            JSONObject json = new JSONObject(text);
            if (!"miot-panel".equals(json.optString("service"))) {
                return null;
            }
            Panel panel = new Panel();
            panel.host = packet.getAddress().getHostAddress();
            panel.port = json.optInt("port", 80);
            panel.name = json.optString("name", panel.host);
            panel.version = json.optString("version", "");
            panel.pin = json.optBoolean("pin", false);
            return panel.host.length() > 0 && panel.port > 0 ? panel : null;
        } catch (Exception ignored) {
            return null;
        }
    }

    private static List<InetAddress> broadcastAddresses() {
        List<InetAddress> result = new ArrayList<InetAddress>();
        try {
            result.add(InetAddress.getByName("255.255.255.255"));
        } catch (Exception ignored) {
        }
        try {
            List<NetworkInterface> interfaces = Collections.list(
                    NetworkInterface.getNetworkInterfaces());
            for (int i = 0; i < interfaces.size(); i++) {
                NetworkInterface ni = interfaces.get(i);
                if (ni.isLoopback() || !ni.isUp()) {
                    continue;
                }
                List<InterfaceAddress> addresses = ni.getInterfaceAddresses();
                for (int j = 0; j < addresses.size(); j++) {
                    InetAddress broadcast = addresses.get(j).getBroadcast();
                    if (broadcast != null && !result.contains(broadcast)) {
                        result.add(broadcast);
                    }
                }
            }
        } catch (Exception ignored) {
        }
        return result;
    }

    private static String localPrefix() {
        try {
            List<NetworkInterface> interfaces = Collections.list(
                    NetworkInterface.getNetworkInterfaces());
            for (int i = 0; i < interfaces.size(); i++) {
                NetworkInterface ni = interfaces.get(i);
                if (ni.isLoopback() || !ni.isUp()) {
                    continue;
                }
                List<InterfaceAddress> addresses = ni.getInterfaceAddresses();
                for (int j = 0; j < addresses.size(); j++) {
                    InterfaceAddress item = addresses.get(j);
                    InetAddress address = item.getAddress();
                    if (address == null || address.getAddress() == null ||
                            address.getAddress().length != 4) {
                        continue;
                    }
                    byte[] raw = address.getAddress();
                    int first = raw[0] & 0xff;
                    if (first == 127 || first == 169) {
                        continue;
                    }
                    return (raw[0] & 0xff) + "." + (raw[1] & 0xff) + "." +
                            (raw[2] & 0xff) + ".";
                }
            }
        } catch (Exception ignored) {
        }
        return null;
    }
}
