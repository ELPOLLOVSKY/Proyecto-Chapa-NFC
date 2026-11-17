plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.chapa.totem"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.chapa.totem"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    // Puerto serie USB (Arduino Uno/Nano con CH340, CP210x, FTDI o ATmega16U2).
    // Verifica en https://jitpack.io/#mik3y/usb-serial-for-android la versión más reciente.
    implementation("com.github.mik3y:usb-serial-for-android:3.7.3")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
