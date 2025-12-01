from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Length, Email, EqualTo, Regexp, ValidationError

class LoginForm(FlaskForm):
    email = StringField('Email', validators=[
        DataRequired(),
        Email(message='Enter a valid email address.')
    ])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Log In')


class RegisterForm(FlaskForm):
    first_name = StringField('First Name', validators=[
        DataRequired(), 
        Length(min=2, max=30, message="First name must be 2-30 characters")
    ])

    last_name = StringField('Last Name', validators=[
        DataRequired(), 
        Length(min=2, max=30, message="Last name must be 2-30 characters")
    ])

    email = StringField('Email', validators=[
        DataRequired(),
        Email(message='Enter a valid email address.'),
        Regexp(r'^[\w\.-]+@[\w\.-]+\.\w+$', message="Invalid email format.")
    ])

    password = PasswordField('Password', validators=[
        DataRequired(), 
        Length(min=8, message="Password must be at least 8 characters"),
        Regexp(
            r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)',
            message="Password must contain uppercase, lowercase, and numbers"
        )
    ])

    confirm_password = PasswordField('Confirm Password', validators=[
        DataRequired(), 
        EqualTo('password', message='Passwords must match.')
    ])

    submit = SubmitField('Register')
    
    def validate_email(self, field):
        """Custom validator to check for SQL injection patterns"""
        dangerous_chars = ["'", '"', ';', '--', '/*', '*/']
        for char in dangerous_chars:
            if char in field.data:
                raise ValidationError("Invalid characters in email")