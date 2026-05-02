#!/bin/bash
# download libiec61850 and compile library
# https://github.com/mz-automation/libiec61850

# steps taken from https://github.com/keyvdir/pyiec61850 and 
# https://github.com/mz-automation/libiec61850/blob/v1.6/pyiec61850/README.md#setup-development-environment-on-linux-ubuntu
git config --global http.sslVerify false
git clone https://github.com/mz-automation/libiec61850.git || echo "directory libiec61850 already exists"

echo "download mbedTLS 3.6.0 dependency"
cd libiec61850/third_party/mbedtls
wget https://github.com/Mbed-TLS/mbedtls/archive/refs/tags/v3.6.0.tar.gz --no-check-certificate || exit 1
tar -xzf v3.6.0.tar.gz
cd ../..

echo "compile libiec61850 library"
cmake -DBUILD_PYTHON_BINDINGS=ON . || exit 1
make WITH_MBEDTLS=1 -j8

echo "install libiec61850 library"
sudo make install
make test

echo "update ld cache"
sudo ldconfig

cd ..