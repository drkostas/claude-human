// vhid_type types text read from stdin through Karabiner's DriverKit virtual HID keyboard.
//
// macOS treats that keyboard as real hardware, so its keys reach places that ignore synthetic
// events: the lock screen and SecurityAgent password panels (secure input mode).
//
// Usage: vhid_type [--wake] [--clear] [--return] < text
//   --wake    tap Shift first and wait, so the lock screen shows and focuses its password field
//   --clear   press Cmd+A and Backspace before typing, so leftover text cannot join the password
//   --return  press Return after the text
//
// The text is read from stdin, never from argv, so it does not appear in the process list.
// Trailing newlines are dropped. Only a US keyboard layout of printable ASCII, Tab and newline is
// supported; other characters are skipped and reported on stderr as "skip".
// It must run as root (sudo), because the daemon's control socket is root only.
// Prints "typed <n> chars" and "OK" and exits 0, or prints "TIMEOUT" and exits 1 after 15 seconds.
#include <atomic>
#include <filesystem>
#include <iostream>
#include <string>
#include <csignal>
#include <thread>
#include <pqrs/karabiner/driverkit/virtual_hid_device_driver.hpp>
#include <pqrs/karabiner/driverkit/virtual_hid_device_service.hpp>
#include <pqrs/local_datagram.hpp>

namespace vhid = pqrs::karabiner::driverkit::virtual_hid_device_driver::hid_report;
namespace svc  = pqrs::karabiner::driverkit::virtual_hid_device_service;
namespace { std::atomic<bool> exit_flag(false); }

static bool char_to_usage(char c, uint16_t& u, bool& shift) {
  shift = false;
  if (c >= 'a' && c <= 'z') { u = 0x04 + (c - 'a'); return true; }
  if (c >= 'A' && c <= 'Z') { u = 0x04 + (c - 'A'); shift = true; return true; }
  if (c >= '1' && c <= '9') { u = 0x1E + (c - '1'); return true; }
  if (c == '0') { u = 0x27; return true; }
  switch (c) {
    case ' ': u=0x2C; return true; case '\n': u=0x28; return true; case '\t': u=0x2B; return true;
    case '-': u=0x2D; return true; case '=': u=0x2E; return true; case '[': u=0x2F; return true;
    case ']': u=0x30; return true; case '\\': u=0x31; return true; case ';': u=0x33; return true;
    case '\'': u=0x34; return true; case '`': u=0x35; return true; case ',': u=0x36; return true;
    case '.': u=0x37; return true; case '/': u=0x38; return true;
    case '!': u=0x1E; shift=true; return true; case '@': u=0x1F; shift=true; return true;
    case '#': u=0x20; shift=true; return true; case '$': u=0x21; shift=true; return true;
    case '%': u=0x22; shift=true; return true; case '^': u=0x23; shift=true; return true;
    case '&': u=0x24; shift=true; return true; case '*': u=0x25; shift=true; return true;
    case '(': u=0x26; shift=true; return true; case ')': u=0x27; shift=true; return true;
    case '_': u=0x2D; shift=true; return true; case '+': u=0x2E; shift=true; return true;
    case '{': u=0x2F; shift=true; return true; case '}': u=0x30; shift=true; return true;
    case '|': u=0x31; shift=true; return true; case ':': u=0x33; shift=true; return true;
    case '"': u=0x34; shift=true; return true; case '~': u=0x35; shift=true; return true;
    case '<': u=0x36; shift=true; return true; case '>': u=0x37; shift=true; return true;
    case '?': u=0x38; shift=true; return true;
  }
  return false;
}

int main(int argc, char** argv) {
  std::string text((std::istreambuf_iterator<char>(std::cin)), std::istreambuf_iterator<char>());
  while (!text.empty() && (text.back()=='\n' || text.back()=='\r')) text.pop_back();
  bool press_return = false;
  bool wake=false;
  bool clear=false;
  for (int i=1;i<argc;i++){ std::string a(argv[i]); if(a=="--return")press_return=true; else if(a=="--wake")wake=true; else if(a=="--clear")clear=true; }

  std::signal(SIGINT, [](int){ exit_flag = true; });
  pqrs::dispatcher::extra::initialize_shared_dispatcher();
  auto client = std::make_unique<svc::client>();
  std::atomic<bool> typed(false), started(false);

  client->connected.connect([&client] {
    std::cout << "connected" << std::endl;
    svc::virtual_hid_keyboard_parameters p;
    p.set_country_code(pqrs::hid::country_code::us);
    client->async_virtual_hid_keyboard_initialize(p);
  });
  client->connect_failed.connect([](auto&& e){ std::cerr << "connect_failed " << e << std::endl; });
  client->virtual_hid_keyboard_ready.connect([&](auto&& ready){
    if (ready && !started.exchange(true)) {
      std::thread([&]{
        std::this_thread::sleep_for(std::chrono::milliseconds(400));
        if (wake) {
          vhid::keyboard_input sd; sd.modifiers.insert(vhid::modifier::left_shift); client->async_post_report(sd);
          std::this_thread::sleep_for(std::chrono::milliseconds(60));
          vhid::keyboard_input su; client->async_post_report(su);
          std::this_thread::sleep_for(std::chrono::milliseconds(900));
        }
        auto tap = [&](uint16_t u, bool shift){
          vhid::keyboard_input down;
          if (shift) down.modifiers.insert(vhid::modifier::left_shift);
          down.keys.insert(u);
          client->async_post_report(down);
          std::this_thread::sleep_for(std::chrono::milliseconds(28));
          vhid::keyboard_input up;               // an empty report releases every key
          client->async_post_report(up);
          std::this_thread::sleep_for(std::chrono::milliseconds(28));
        };
        // Clearing first means only the given text ends up in the field. Leftover text would make
        // the password wrong and cost a failed attempt (and after a few, a macOS lockout delay).
        auto tapMod = [&](vhid::modifier m, uint16_t u){
          vhid::keyboard_input down; down.modifiers.insert(m); down.keys.insert(u);
          client->async_post_report(down);
          std::this_thread::sleep_for(std::chrono::milliseconds(28));
          vhid::keyboard_input up; client->async_post_report(up);
          std::this_thread::sleep_for(std::chrono::milliseconds(28));
        };
        if (clear) {
          tapMod(vhid::modifier::left_command, 0x04);   // Cmd+A
          std::this_thread::sleep_for(std::chrono::milliseconds(50));
          tap(0x2A, false); tap(0x2A, false); tap(0x2A, false);   // Backspace x3
          std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
        for (char c : text) {
          uint16_t u; bool shift;
          if (char_to_usage(c, u, shift)) tap(u, shift);
          else std::cerr << "skip\n";
        }
        if (press_return) tap(0x28, false);
        std::cout << "typed " << text.size() << " chars" << std::endl;
        std::this_thread::sleep_for(std::chrono::milliseconds(300));
        typed = true; exit_flag = true;
      }).detach();
    }
  });

  client->async_start();
  int w=0; while (!exit_flag && w<150) { std::this_thread::sleep_for(std::chrono::milliseconds(100)); w++; }
  std::this_thread::sleep_for(std::chrono::milliseconds(200));
  client = nullptr;
  pqrs::dispatcher::extra::terminate_shared_dispatcher();
  std::cout << (typed ? "OK" : "TIMEOUT") << std::endl;
  return typed ? 0 : 1;
}
