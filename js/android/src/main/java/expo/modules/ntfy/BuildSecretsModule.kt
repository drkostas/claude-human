package expo.modules.ntfy

import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition

/**
 * The secrets the app's build wrote into its string resources (the build-secrets config plugin), read by name at
 * run time, so the app's JavaScript bundle carries none of them and an over-the-air update of the JavaScript holds
 * no secret. A name the build did not write, or one that is not a plain name, reads as null.
 */
class BuildSecretsModule : Module() {
  override fun definition() = ModuleDefinition {
    Name("ExpoNtfyBuildSecrets")

    Function("get") { name: String ->
      val context = appContext.reactContext
      val id = if (context == null || !NAME.matches(name)) 0
        else context.resources.getIdentifier(PREFIX + name, "string", context.packageName)
      if (context == null || id == 0) null else context.getString(id)
    }
  }

  private companion object {
    const val PREFIX = "build_secret_"
    val NAME = Regex("[A-Za-z][A-Za-z0-9_]*")
  }
}
