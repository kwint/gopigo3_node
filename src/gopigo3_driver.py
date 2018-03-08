#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function
import sys

try:
    import gopigo3
except IOError as e:
    print("cannot find SPI device")
    sys.exit()

import rospy
from std_msgs.msg import UInt8, Int8, Int16, Float64
from std_msgs.msg import ColorRGBA
from std_msgs.msg import Header
from std_srvs.srv import Trigger
from geometry_msgs.msg import Twist
from geometry_msgs.msg import PoseWithCovariance, TwistWithCovariance
from geometry_msgs.msg import Transform, TransformStamped
from nav_msgs.msg import Odometry
from gopigo3_node.msg import MotorStatusLR, MotorStatus
from gopigo3_node.srv import SPI, SPIResponse
from tf.transformations import quaternion_about_axis, quaternion_multiply
from tf.broadcaster import TransformBroadcaster
import numpy as np
import os
import time


class Robot:
    # short variables
    ML = gopigo3.GoPiGo3.MOTOR_LEFT
    MR = gopigo3.GoPiGo3.MOTOR_RIGHT
    S1 = gopigo3.GoPiGo3.SERVO_1
    S2 = gopigo3.GoPiGo3.SERVO_2
    BL = gopigo3.GoPiGo3.LED_BLINKER_LEFT
    BR = gopigo3.GoPiGo3.LED_BLINKER_RIGHT
    EL = gopigo3.GoPiGo3.LED_EYE_LEFT
    ER = gopigo3.GoPiGo3.LED_EYE_RIGHT
    EW = gopigo3.GoPiGo3.LED_WIFI
    WIDTH = gopigo3.GoPiGo3.WHEEL_BASE_WIDTH
    CIRCUMFERENCE = gopigo3.GoPiGo3.WHEEL_CIRCUMFERENCE*1e-3

    POWER_PIN = "23"

    def __init__(self):
        #### GoPiGo3 power management
        # export pin
        if not os.path.isdir("/sys/class/gpio/gpio"+self.POWER_PIN):
            gpio_export = os.open("/sys/class/gpio/export", os.O_WRONLY)
            os.write(gpio_export, self.POWER_PIN.encode())
            os.close(gpio_export)
        time.sleep(0.1)

        # set pin direction
        gpio_direction = os.open("/sys/class/gpio/gpio"+self.POWER_PIN+"/direction", os.O_WRONLY)
        os.write(gpio_direction, "out".encode())
        os.close(gpio_direction)

        # activate power management
        self.gpio_value = os.open("/sys/class/gpio/gpio"+self.POWER_PIN+"/value", os.O_WRONLY)
        os.write(self.gpio_value, "1".encode())

        # GoPiGo3 and ROS setup
        self.g = gopigo3.GoPiGo3()
        print("GoPiGo3 info:")
        print("Manufacturer    : ", self.g.get_manufacturer())
        print("Board           : ", self.g.get_board())
        print("Serial Number   : ", self.g.get_id())
        print("Hardware version: ", self.g.get_version_hardware())
        print("Firmware version: ", self.g.get_version_firmware())

        self.reset_odometry()

        rospy.init_node("gopigo3")

        self.br = TransformBroadcaster()

        # subscriber
        rospy.Subscriber("motor/dps/left", Int16, lambda msg: self.g.set_motor_dps(self.ML, msg.data))
        rospy.Subscriber("motor/dps/right", Int16, lambda msg: self.g.set_motor_dps(self.MR, msg.data))
        rospy.Subscriber("motor/pwm/left", Int8, lambda msg: self.g.set_motor_power(self.ML, msg.data))
        rospy.Subscriber("motor/pwm/right", Int8, lambda msg: self.g.set_motor_power(self.MR, msg.data))
        rospy.Subscriber("motor/position/left", Int16, lambda msg: self.g.set_motor_position(self.ML, msg.data))
        rospy.Subscriber("motor/position/right", Int16, lambda msg: self.g.set_motor_position(self.MR, msg.data))
        rospy.Subscriber("servo/1", Float64, lambda msg: self.g.set_servo(self.S1, msg.data*16666))
        rospy.Subscriber("servo/2", Float64, lambda msg: self.g.set_servo(self.S2, msg.data*16666))
        rospy.Subscriber("cmd_vel", Twist, self.on_twist)

        rospy.Subscriber("led/blinker/left", UInt8, lambda msg: self.g.set_led(self.BL, msg.data))
        rospy.Subscriber("led/blinker/right", UInt8, lambda msg: self.g.set_led(self.BR, msg.data))
        rospy.Subscriber("led/eye/left", ColorRGBA, lambda c: self.g.set_led(self.EL, int(c.r*255), int(c.g*255), int(c.b*255)))
        rospy.Subscriber("led/eye/right", ColorRGBA, lambda c: self.g.set_led(self.ER, int(c.r*255), int(c.g*255), int(c.b*255)))
        rospy.Subscriber("led/wifi", ColorRGBA, lambda c: self.g.set_led(self.EW, int(c.r * 255), int(c.g * 255), int(c.b * 255)))

        # publisher
        self.pub_enc_l = rospy.Publisher('motor/encoder/left', Float64, queue_size=10)
        self.pub_enc_r = rospy.Publisher('motor/encoder/right', Float64, queue_size=10)
        self.pub_battery = rospy.Publisher('battery_voltage', Float64, queue_size=10)
        self.pub_motor_status = rospy.Publisher('motor/status', MotorStatusLR, queue_size=10)
        self.pub_odometry = rospy.Publisher("odometry", Odometry, queue_size=10)

        # services
        self.srv_reset = rospy.Service('reset', Trigger, self.reset)
        self.srv_spi = rospy.Service('spi', SPI, lambda req: SPIResponse(data_in=self.g.spi_transfer_array(req.data_out)))
        self.srv_pwr_on = rospy.Service('power/on', Trigger, self.power_on)
        self.srv_pwr_off = rospy.Service('power/off', Trigger, self.power_off)

        # main loop
        rate = rospy.Rate(10)   # in Hz
        while not rospy.is_shutdown():
            self.pub_enc_l.publish(Float64(data=self.g.get_motor_encoder(self.ML)))
            self.pub_enc_r.publish(Float64(data=self.g.get_motor_encoder(self.MR)))
            self.pub_battery.publish(Float64(data=self.g.get_voltage_battery()))

            # publish motor status, including encoder value
            (flags, power, encoder, speed) = self.g.get_motor_status(self.ML)
            status_left = MotorStatus(low_voltage=(flags & (1<<0)), overloaded=(flags & (1<<1)),
                                      power=power, encoder=encoder, speed=speed)
            (flags, power, encoder, speed) = self.g.get_motor_status(self.MR)
            status_right = MotorStatus(low_voltage=(flags & (1<<0)), overloaded=(flags & (1<<1)),
                                      power=power, encoder=encoder, speed=speed)
            self.pub_motor_status.publish(MotorStatusLR(header=Header(stamp=rospy.Time.now()), left=status_left, right=status_right))

            # publish current pose
            (odom, transform)= self.odometry(status_left, status_right)
            self.pub_odometry.publish(odom)
            self.br.sendTransformMessage(transform)

            rate.sleep()

        self.g.reset_all()

        # deactivate power management
        os.write(self.gpio_value, "0".encode())
        os.close(self.gpio_value)

        # unexport pin
        if os.path.isdir("/sys/class/gpio/gpio" + self.POWER_PIN):
            gpio_export = os.open("/sys/class/gpio/unexport", os.O_WRONLY)
            os.write(gpio_export, self.POWER_PIN.encode())
            os.close(gpio_export)

    def reset_odometry(self):
        self.last_encoders = {'l': 0, 'r': 0}
        self.pose = PoseWithCovariance()
        self.pose.pose.orientation.w = 1

    def reset(self, req):
        self.g.reset_all()
        self.reset_odometry()
        return [True, ""]

    def power_on(self, req):
        os.write(self.gpio_value, "1".encode())
        return [True, "Power ON"]

    def power_off(self, req):
        os.write(self.gpio_value, "0".encode())
        return [True, "Power OFF"]

    def on_twist(self, twist):
        # Compute left and right wheel speed from a twist, which is the combination
        # of a linear speed (m/s) and an angular speed (rad/s).
        # In the coordinate frame of the GoPiGo3, the x-axis is pointing forward
        # and the z-axis is pointing upwards. Since the GoPiGo3 is only moving within
        # the x-y-plane, we are only using the linear velocity in x direction (forward)
        # and the angular velocity around the z-axis (yaw).
        # source:
        #   https://opencurriculum.org/5481/circular-motion-linear-and-angular-speed/
        #   http://www.euclideanspace.com/physics/kinematics/combinedVelocity/index.htm

        right_speed = twist.linear.x + twist.angular.z * self.WIDTH / 2
        left_speed = twist.linear.x - twist.angular.z * self.WIDTH / 2

        self.g.set_motor_dps(self.ML, left_speed/self.CIRCUMFERENCE*360)
        self.g.set_motor_dps(self.MR, right_speed/self.CIRCUMFERENCE*360)

    def odometry(self, left, right):
        # Compute current linear and angular speed from wheel speed
        twist = TwistWithCovariance()
        twist.twist.linear.x = (right.speed - left.speed) / 2.0
        twist.twist.angular.z = (right.speed - left.speed) / self.WIDTH

        # Compute position and orientation from travelled distance per wheel
        # http://www8.cs.umu.se/kurser/5DV122/HT13/material/Hellstrom-ForwardKinematics.pdf
        # position change per wheel in meter
        dl = (left.encoder - self.last_encoders['l']) / 360.0 * self.CIRCUMFERENCE
        dr = (right.encoder - self.last_encoders['r']) / 360.0 * self.CIRCUMFERENCE

        if dr!=dl:
            radius = self.WIDTH/2.0 * (dl+dr) / (dr-dl)
            angle = (dr-dl) / self.WIDTH
        else:
            radius = 0
            angle = 0

        # new position
        pos = np.array([self.pose.pose.position.x, self.pose.pose.position.y])
        icc = pos + radius * np.array([-np.sin(angle), np.cos(angle)])
        new_pos = np.dot(np.array([[np.cos(angle), -np.sin(angle)],[np.sin(angle), np.cos(angle)]]),  pos-icc) + icc

        # update pose
        new_q = quaternion_multiply(
            quaternion_about_axis(angle, (0, 0, 1)),
            [self.pose.pose.orientation.x, self.pose.pose.orientation.y, self.pose.pose.orientation.z, self.pose.pose.orientation.w])
        self.pose.pose.orientation.x = new_q[0]
        self.pose.pose.orientation.y = new_q[1]
        self.pose.pose.orientation.z = new_q[2]
        self.pose.pose.orientation.w = new_q[3]
        self.pose.pose.position.x = new_pos[0]
        self.pose.pose.position.y = new_pos[1]

        # set previous encoder state
        self.last_encoders['l'] = left.encoder
        self.last_encoders['r'] = right.encoder

        odom = Odometry(header=Header(frame_id="gopigo"), child_frame_id="world",
                        pose=self.pose, twist=twist)

        transform = TransformStamped(header=Header(frame_id="gopigo"), child_frame_id="world")
        transform.transform.translation.x = self.pose.pose.position.x
        transform.transform.translation.y = self.pose.pose.position.y
        transform.transform.translation.z = self.pose.pose.position.z
        transform.transform.rotation = self.pose.pose.orientation

        return odom, transform


if __name__ == '__main__':
    try:
        Robot()
    except rospy.ROSInterruptException:
        pass
