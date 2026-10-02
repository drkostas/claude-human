const fs = require("fs");
const path = require("path");
const { getDefaultConfig } = require("expo/metro-config");

const config = getDefaultConfig(__dirname);

// The two packages are file: dependencies on folders of this repository, so node_modules holds a
// symlink to a folder outside this app. Metro must watch the real folder, and the packages' own
// imports (react, react-native, expo-notifications) must resolve from this app's node_modules,
// never from a second copy.
for (const name of ["@drkostas/claude-human-client", "@drkostas/expo-ntfy"]) {
  const link = path.join(__dirname, "node_modules", name);
  if (fs.existsSync(link)) {
    config.watchFolders = [...(config.watchFolders || []), fs.realpathSync(link)];
  }
}
config.resolver.nodeModulesPaths = [
  path.join(__dirname, "node_modules"),
  ...(config.resolver.nodeModulesPaths || []),
];

module.exports = config;
