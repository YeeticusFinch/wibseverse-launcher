package com.wibsverse.launcherbridge;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import net.minecraft.client.Minecraft;
import net.minecraft.client.multiplayer.ServerData;
import net.minecraftforge.api.distmarker.Dist;
import net.minecraftforge.client.event.ClientChatReceivedEvent;
import net.minecraftforge.event.TickEvent;
import net.minecraftforge.eventbus.api.SubscribeEvent;
import net.minecraftforge.fml.common.Mod;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.Comparator;
import java.util.Locale;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.regex.Pattern;

@Mod(WibsverseLinkBridge.MOD_ID)
public final class WibsverseLinkBridge {
    static final String MOD_ID = "wibsverse_launcher_bridge";
    private static final Gson GSON = new Gson();
    private static final Pattern CODE = Pattern.compile("^[A-Z0-9]{4}(?:-[A-Z0-9]{4}){3}$");
    private static final AtomicBoolean COMMAND_SENT = new AtomicBoolean(false);
    private static volatile LinkRequest pendingResult;
    private static int cleanupTickCounter;

    public WibsverseLinkBridge() {}

    @Mod.EventBusSubscriber(modid = MOD_ID, value = Dist.CLIENT)
    public static final class ClientEvents {
        @SubscribeEvent
        public static void onClientTick(TickEvent.ClientTickEvent event) {
            if (event.phase != TickEvent.Phase.END || ++cleanupTickCounter < 100) return;
            cleanupTickCounter = 0;
            String configuredPath = System.getProperty("wibs.linkaccount.file", "").trim();
            if (configuredPath.isEmpty()) return;
            Path codePath = Path.of(configuredPath);
            LinkRequest request = readRequest(codePath);
            if (request != null && request.expiresAtEpochSeconds <= Instant.now().getEpochSecond()) {
                deleteDirectory(codePath.getParent());
            }
        }

        @SubscribeEvent
        public static void onServerChat(ClientChatReceivedEvent event) {
            String message = event.getMessage().getString().toLowerCase(Locale.ROOT);
            LinkRequest pending = pendingResult;
            if (pending != null) {
                if (message.contains("invalid code") || message.contains("expired code")
                        || message.contains("already linked to another") || message.contains("link code has expired")) {
                    writeStatus(Path.of(System.getProperty("wibs.linkaccount.file", "")).getParent(), pending, "rejected");
                    pendingResult = null;
                    return;
                }
                if (message.contains("successfully linked") || message.contains("minecraft account linked")
                        || message.contains("profile linked successfully") || message.contains("minecraft account added")) {
                    writeStatus(Path.of(System.getProperty("wibs.linkaccount.file", "")).getParent(), pending, "linked");
                    pendingResult = null;
                    return;
                }
            }
            if (!(message.contains("cracked account verification is required")
                    || message.contains("sign in at wibsverse.com")
                    || message.contains("authentication required"))) {
                return;
            }

            Minecraft minecraft = Minecraft.getInstance();
            ServerData server = minecraft.getCurrentServer();
            if (minecraft.player == null || minecraft.getConnection() == null || server == null
                    || !isWibsVerseServer(server.ip)) {
                return;
            }

            String configuredPath = System.getProperty("wibs.linkaccount.file", "").trim();
            if (configuredPath.isEmpty()) {
                return;
            }
            Path codePath = Path.of(configuredPath);
            LinkRequest request = readRequest(codePath);
            if (request == null || !minecraft.player.getGameProfile().getName().equalsIgnoreCase(request.minecraftUsername)
                    || !minecraft.player.getGameProfile().getId().toString().equalsIgnoreCase(request.runtimeUuid)
                    || request.expiresAtEpochSeconds <= Instant.now().getEpochSecond()
                    || !COMMAND_SENT.compareAndSet(false, true)) {
                return;
            }

            minecraft.execute(() -> {
                try {
                    if (minecraft.player == null || minecraft.getConnection() == null) {
                        COMMAND_SENT.set(false);
                        return;
                    }
                    minecraft.player.connection.sendCommand("linkaccount " + request.code);
                    Files.deleteIfExists(codePath);
                    writeStatus(codePath.getParent(), request, "command_sent");
                    pendingResult = request;
                    scheduleCleanup(codePath.getParent(), request.expiresAtEpochSeconds);
                } catch (Exception ignored) {
                    COMMAND_SENT.set(false);
                }
            });
        }

        private static boolean isWibsVerseServer(String address) {
            if (address == null) return false;
            String host = address.trim().toLowerCase(Locale.ROOT);
            int colon = host.lastIndexOf(':');
            if (colon > 0 && host.indexOf(':') == colon) host = host.substring(0, colon);
            return host.equals("play.wibsverse.com") || host.endsWith(".play.wibsverse.com");
        }

        private static LinkRequest readRequest(Path path) {
            try {
                if (Files.isSymbolicLink(path) || !Files.isRegularFile(path) || Files.size(path) > 4096) return null;
                JsonObject data = JsonParser.parseString(Files.readString(path, StandardCharsets.UTF_8)).getAsJsonObject();
                String code = data.get("code").getAsString();
                String username = data.get("minecraft_username").getAsString();
                String session = data.get("session_id").getAsString();
                String runtimeUuid = data.get("runtime_uuid").getAsString();
                long expiry = data.get("expires_at_epoch_seconds").getAsLong();
                if (!CODE.matcher(code).matches() || !username.matches("[A-Za-z0-9_]{3,16}")
                        || !session.matches("[0-9a-fA-F-]{36}")
                        || !runtimeUuid.matches("[0-9a-fA-F-]{36}")) return null;
                return new LinkRequest(code, username, session, runtimeUuid, expiry);
            } catch (Exception ignored) {
                return null;
            }
        }

        private static void writeStatus(Path directory, LinkRequest request, String status) {
            try {
                JsonObject result = new JsonObject();
                result.addProperty("session_id", request.sessionId);
                result.addProperty("minecraft_username", request.minecraftUsername);
                result.addProperty("status", status);
                result.addProperty("at_epoch_seconds", Instant.now().getEpochSecond());
                Path target = directory.resolve("link-status.json");
                Path temporary = directory.resolve("link-status.json.tmp");
                Files.writeString(temporary, GSON.toJson(result), StandardCharsets.UTF_8);
                try {
                    Files.move(temporary, target, java.nio.file.StandardCopyOption.ATOMIC_MOVE,
                            java.nio.file.StandardCopyOption.REPLACE_EXISTING);
                } catch (java.nio.file.AtomicMoveNotSupportedException ignored) {
                    Files.move(temporary, target, java.nio.file.StandardCopyOption.REPLACE_EXISTING);
                }
            } catch (Exception ignored) {
                // Never print the single-use code or authentication data to game logs.
            }
        }

        private static void scheduleCleanup(Path directory, long expiry) {
            if (directory == null) return;
            long delay = Math.max(1, expiry - Instant.now().getEpochSecond() + 30);
            CompletableFuture.delayedExecutor(delay, TimeUnit.SECONDS).execute(() -> {
                pendingResult = null;
                deleteDirectory(directory);
            });
        }

        private static void deleteDirectory(Path directory) {
            if (directory == null || !Files.isDirectory(directory)) return;
            try (var paths = Files.walk(directory)) {
                paths.sorted(Comparator.reverseOrder()).forEach(path -> {
                    try { Files.deleteIfExists(path); } catch (Exception ignored) {}
                });
            } catch (Exception ignored) {}
        }
    }

    private record LinkRequest(String code, String minecraftUsername, String sessionId,
                               String runtimeUuid, long expiresAtEpochSeconds) {}
}
