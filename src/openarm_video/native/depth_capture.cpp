// OpenArm's adapter source. OpenNI2 headers and Orbbec drivers are external.
#include <OpenNI.h>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

static volatile std::sig_atomic_t running = 1;
static void stop(int) { running = 0; }
static void check(openni::Status status, const char* operation) {
    if (status != openni::STATUS_OK)
        throw std::runtime_error(std::string(operation) + ": " + openni::OpenNI::getExtendedError());
}

int main(int argc, char** argv) {
    if (argc != 6) {
        std::cerr << "usage: capture OUTPUT.pgm WIDTH HEIGHT FPS URI_OR_EMPTY\n";
        return 2;
    }
    std::signal(SIGTERM, stop);
    std::signal(SIGINT, stop);
    openni::Device device;
    openni::VideoStream stream;
    int result = 0;
    try {
        const int width = std::stoi(argv[2]), height = std::stoi(argv[3]), fps = std::stoi(argv[4]);
        if (width <= 0 || width > 4096 || height <= 0 || height > 4096 || fps <= 0 || fps > 120)
            throw std::runtime_error("Invalid depth mode");
        check(openni::OpenNI::initialize(), "initialize OpenNI2");
        // No permanent device settings, registration, or firmware changes.
        openni::OpenNI::setLogMinSeverity(3);
        const char* uri = argv[5];
        openni::Array<openni::DeviceInfo> devices;
        if (!*uri) {
            openni::OpenNI::enumerateDevices(&devices);
            int found = 0;
            for (int i = 0; i < devices.getSize(); ++i) {
                if (devices[i].getUsbVendorId() == 0x2bc5 && devices[i].getUsbProductId() == 0x0403) {
                    uri = devices[i].getUri();
                    ++found;
                }
            }
            if (found != 1)
                throw std::runtime_error("Expected one Astra Pro depth device (2bc5:0403); specify URI for multiple devices");
        }
        check(device.open(uri), "open depth device");
        check(stream.create(device, openni::SENSOR_DEPTH), "create depth stream");
        openni::VideoMode mode;
        mode.setResolution(width, height);
        mode.setFps(fps);
        mode.setPixelFormat(openni::PIXEL_FORMAT_DEPTH_1_MM);
        check(stream.setVideoMode(mode), "set depth mode in millimetres");
        check(stream.setMirroringEnabled(false), "disable depth mirroring");
        check(stream.start(), "start depth");
        openni::VideoStream* streams[] = {&stream};
        openni::VideoFrameRef frame;
        int warmup = 0;
        while (running) {
            int ready = -1;
            check(openni::OpenNI::waitForAnyStream(streams, 1, &ready, 3000), "wait for depth frame");
            check(stream.readFrame(&frame), "read depth frame");
            if (!frame.isValid() || frame.getVideoMode().getPixelFormat() != openni::PIXEL_FORMAT_DEPTH_1_MM)
                throw std::runtime_error("Invalid depth frame or units");
            if (++warmup < 15) continue;
            const std::string path = argv[1], temporary = path + ".tmp";
            std::ofstream out(temporary, std::ios::binary);
            out << "P5\n" << frame.getWidth() << " " << frame.getHeight() << "\n65535\n";
            std::vector<char> row_bytes(frame.getWidth() * 2);
            for (int y = 0; y < frame.getHeight(); ++y) {
                const auto* row = reinterpret_cast<const std::uint16_t*>(
                    static_cast<const std::uint8_t*>(frame.getData()) + y * frame.getStrideInBytes());
                for (int x = 0; x < frame.getWidth(); ++x) {
                    row_bytes[x * 2] = static_cast<char>(row[x] >> 8);
                    row_bytes[x * 2 + 1] = static_cast<char>(row[x] & 255);
                }
                out.write(row_bytes.data(), row_bytes.size());
            }
            out.close();
            if (!out || std::rename(temporary.c_str(), path.c_str()))
                throw std::runtime_error("Cannot publish depth frame");
        }
    } catch (const std::exception& error) {
        std::cerr << error.what() << std::endl;
        result = 1;
    }
    if (stream.isValid()) { stream.stop(); stream.destroy(); }
    if (device.isValid()) device.close();
    openni::OpenNI::shutdown();
    return result;
}
